"""connect_vr921.py

Diagnostic SHIP/SPINE client for Vaillant VR921/EEBUS devices.

High-level flow (what this script does):
1) Create or reuse a persistent client certificate (identity is the certificate SKI).
2) Announce a local SHIP service via mDNS (`_ship._tcp.local.`) so the Vaillant app/device
     can discover the client for trust/pairing.
3) Discover the VR921 via mDNS and connect to its SHIP websocket (`wss://<ip>:<port>/ship/`).
4) Perform the SHIP handshake (HELLO, protocol selection, PIN state, access methods).
5) Exchange SPINE datagrams inside SHIP DATA frames:
     - Reply to gateway-initiated READ requests (keeps the session alive)
     - Request remote detailed discovery and use-case data
     - Inventory advertised feature capabilities and issue read-only requests
     - Normalize Measurement, Setpoint and additional diagnostic/energy data

Home Assistant / MQTT:
- If `HA_MQTT_HOST` (or mqtt_secrets.py) is configured, the script publishes Home Assistant
    MQTT Discovery config + state updates.

Interoperability notes:
- SHIP/EEBUS JSON is often "array-wrapped" (list of single-key objects). Helpers in this file
    convert between standard JSON and that on-wire format.
- Vaillant devices can be timing/format strict; a lot of logic here is defensive.
"""

from __future__ import annotations

import asyncio
import datetime
import inspect
import logging
import json
import os
import re
import socket
import ssl
import sys
import tempfile
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple, cast
from zeroconf.asyncio import AsyncZeroconf, AsyncServiceInfo, AsyncServiceBrowser
from zeroconf import IPVersion, ServiceListener
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
_builtin_print = print


def _human_print(*args: Any, **kwargs: Any) -> None:
    """Write human diagnostics to stderr so stdout can remain valid JSONL."""
    kwargs.setdefault("file", sys.stderr)
    _builtin_print(*args, **kwargs)


def _emit_jsonl(payload: Dict[str, Any]) -> None:
    """Emit exactly one machine-readable JSON object on stdout."""
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


class PeerIdentityError(RuntimeError):
    """Raised when the discovered and TLS peer identities do not match."""


LOCAL_CLIENT_FEATURES: Dict[str, Tuple[Tuple[int, ...], int]] = {
    # Preserve the field-used Measurement address and add client endpoints for
    # additional read-only server feature families.
    "Measurement": ((1,), 1),
    "Sensing": ((1,), 2),
    "Setpoint": ((1,), 3),
    "HVAC": ((1,), 4),
    "SmartEnergyManagementPs": ((1,), 5),
    "DeviceDiagnosis": ((1,), 6),
    "ElectricalConnection": ((1,), 7),
    "DeviceClassification": ((1,), 8),
    "NodeManagement": ((0,), 0),
}


SAFE_READ_FUNCTIONS: Dict[str, frozenset[str]] = {
    "NodeManagement": frozenset(
        {
            "nodeManagementSubscriptionData",
            "nodeManagementUseCaseData",
        }
    ),
    "DeviceClassification": frozenset(
        {
            "deviceClassificationManufacturerData",
            "deviceClassificationUserData",
        }
    ),
    "Measurement": frozenset(
        {
            "measurementConstraintsListData",
            "measurementDescriptionListData",
            "measurementListData",
            "measurementSeriesListData",
            "measurementThresholdRelationListData",
        }
    ),
    "Setpoint": frozenset(
        {
            "setpointConstraintsListData",
            "setpointDescriptionListData",
            "setpointListData",
        }
    ),
    "HVAC": frozenset(
        {
            "hvacOperationModeDescriptionListData",
            "hvacOverrunDescriptionListData",
            "hvacOverrunListData",
            "hvacSystemFunctionDescriptionListData",
            "hvacSystemFunctionListData",
            "hvacSystemFunctionOperationModeRelationListData",
            "hvacSystemFunctionPowerSequenceRelationListData",
            "hvacSystemFunctionSetpointRelationListData",
        }
    ),
    "SmartEnergyManagementPs": frozenset(
        {
            "smartEnergyManagementPsData",
            "smartEnergyManagementPsPriceData",
        }
    ),
    "DeviceDiagnosis": frozenset(
        {
            "deviceDiagnosisHeartbeatData",
            "deviceDiagnosisServiceData",
            "deviceDiagnosisStateData",
        }
    ),
    "ElectricalConnection": frozenset(
        {
            "electricalConnectionCharacteristicListData",
            "electricalConnectionDescriptionListData",
            "electricalConnectionParameterDescriptionListData",
            "electricalConnectionPermittedValueSetListData",
            "electricalConnectionStateListData",
        }
    ),
}


@dataclass(frozen=True)
class FeatureCapability:
    entity: Tuple[int, ...]
    feature: int
    feature_type: str
    role: str
    operations_by_function: Dict[str, frozenset[str]] = field(compare=False)

    @property
    def key(self) -> Tuple[Tuple[int, ...], int]:
        return self.entity, self.feature

    def readable_functions(self, *, allow_all_advertised: bool = False) -> Tuple[str, ...]:
        advertised = {
            function
            for function, operations in self.operations_by_function.items()
            if "read" in operations
        }
        if allow_all_advertised:
            return tuple(sorted(advertised))
        return tuple(sorted(advertised.intersection(SAFE_READ_FUNCTIONS.get(self.feature_type, frozenset()))))


@dataclass
class PendingRequest:
    counter: int
    function: str
    target: Tuple[Tuple[int, ...], int]
    sent_at: float
    kind: str = "read"
    ack_received: bool = False


# ---------------------------------------------------------------------------
# Utilities / small helpers
# ---------------------------------------------------------------------------


class MsgCounter:
    """Async-safe SPINE msgCounter generator.

    SPINE datagrams contain a monotonically increasing msgCounter.
    We guard increments with a lock because this script may send messages from
    different async code paths.
    """

    def __init__(self, start: int = 1):
        self._value = start
        self._lock = asyncio.Lock()

    async def next(self) -> int:
        async with self._lock:
            value = self._value
            self._value += 1
            # SPINE msgCounter is uint64 and may overflow; for this diagnostic client,
            # a simple increment is sufficient.
            return value


def _env_str(name: str, default: str = "") -> str:
    """Read an environment variable as a string."""
    v = os.environ.get(name)
    if v is None:
        return default
    return str(v)


def _env_int(name: str, default: int) -> int:
    """Read an environment variable as an int (with fallback)."""
    v = os.environ.get(name)
    if v is None:
        return default
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    """Read an environment variable as a boolean.

    Accepts common truthy/falsey strings like 1/0, true/false, yes/no.
    """
    v = os.environ.get(name)
    if v is None:
        return default
    s = str(v).strip().lower()
    if s in {"1", "true", "yes", "y", "on"}:
        return True
    if s in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _main_pairing_mdns_identity(local_ski: str) -> Tuple[str, Dict[str, str]]:
    """Return the exact service name and TXT record used by `main` for pairing."""
    normalized_ski = _normalize_ski(local_ski)
    return f"Python-{normalized_ski[:6]}", {
        "txtvers": "1",
        "path": "/ship/",
        "ski": normalized_ski,
        "register": "true",
    }


def _slug(s: str) -> str:
    """Turn a string into a safe MQTT/Home-Assistant object_id component."""
    s = s.strip().lower()
    s = re.sub(r"[^a-z0-9_]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "unknown"


def _unit_to_ha(unit: Any) -> str:
    """Normalize SPINE unit representations to Home Assistant-friendly strings."""
    if unit is None:
        return ""
    if isinstance(unit, str):
        u = unit.strip()
        # SPINE/EEBUS commonly uses symbolic unit strings (e.g. "degC").
        # Home Assistant expects specific unit strings for some device classes.
        if u == "degC":
            return "°C"
        if u == "degF":
            return "°F"
        return u
    if isinstance(unit, dict):
        u = unit.get("unit") or unit.get("name")
        return _unit_to_ha(str(u)) if u is not None else ""
    return str(unit)


def _guess_ha_metadata(scope_type: str, unit: str, measurement_type: str = "") -> Dict[str, str]:
    """Best-effort mapping to Home Assistant sensor metadata.

    Keep it simple; HA can still show the sensor without these.
    """
    s = (scope_type or "").lower()
    measurement = (measurement_type or "").lower()
    u = (unit or "").strip()

    if measurement == "temperature" or "temperature" in s:
        return {"device_class": "temperature", "state_class": "measurement", "unit": u or "°C"}
    if measurement == "power" or "power" in s:
        return {"device_class": "power", "state_class": "measurement", "unit": u or "W"}
    if measurement == "energy" or "energy" in s:
        return {"device_class": "energy", "state_class": "total_increasing", "unit": u or "Wh"}
    if measurement == "current" or "current" in s:
        return {"device_class": "current", "state_class": "measurement", "unit": u or "A"}
    if measurement == "voltage" or "voltage" in s:
        return {"device_class": "voltage", "state_class": "measurement", "unit": u or "V"}
    return {"device_class": "", "state_class": "measurement", "unit": u}


def _friendly_sensor_name(scope_type: str, *, source_entity: Optional[list[int]] = None) -> str:
    """Return a human-friendly sensor name for Home Assistant."""
    s = (scope_type or "").strip()
    low = s.lower()

    # Known Vaillant/VR921 scopes
    if low == "outsideairtemperature":
        return "Außentemperatur"
    if low == "dhwtemperature":
        return "Warmwasser Temperatur"
    if low == "roomairtemperature":
        return "Raumtemperatur"
    if low == "acpowertotal":
        return "Kompressor Leistung"
    if low.startswith("acpower"):
        return "Leistung"
    if "temperature" in low:
        return "Temperatur"

    # Fallback: keep scope, optionally annotate source entity
    if source_entity:
        return f"{s} (entity={source_entity})"
    return s or "Messwert"


class HAMqttPublisher:
    """Optional MQTT publisher for Home Assistant Discovery.

    Enabled when HA_MQTT_HOST is set. Requires paho-mqtt.
    """

    def __init__(self, *, device_id: str, device_name: str):
        self.device_id = device_id
        self.device_name = device_name
        self.base_prefix = _env_str("HA_MQTT_PREFIX", "homeassistant").strip("/")
        self.state_prefix = _env_str("HA_MQTT_STATE_PREFIX", "ship").strip("/")

        # Broker credentials:
        # 1) Prefer local file `mqtt_secrets.py` (keeps secrets out of main script)
        # 2) Allow environment variables to override
        secrets_host = ""
        secrets_port = 1883
        secrets_user = ""
        secrets_password = ""
        try:
            import mqtt_secrets as _mqtt_secrets  # type: ignore

            secrets_host = str(getattr(_mqtt_secrets, "HA_MQTT_HOST", "") or "").strip()
            secrets_port = int(getattr(_mqtt_secrets, "HA_MQTT_PORT", 1883) or 1883)
            secrets_user = str(getattr(_mqtt_secrets, "HA_MQTT_USER", "") or "").strip()
            secrets_password = str(getattr(_mqtt_secrets, "HA_MQTT_PASSWORD", "") or "")
        except ModuleNotFoundError:
            # Missing/invalid secrets file is OK; env vars can still configure MQTT.
            pass
        except (AttributeError, TypeError, ValueError) as exc:
            _human_print(f"⚠️  [MQTT] Ungültige mqtt_secrets.py ignoriert: {exc}")

        # If neither secrets nor env provides a host, MQTT stays disabled.
        self.host = _env_str("HA_MQTT_HOST", secrets_host).strip()
        self.port = _env_int("HA_MQTT_PORT", secrets_port)
        self.username = _env_str("HA_MQTT_USER", secrets_user)
        self.password = _env_str("HA_MQTT_PASSWORD", secrets_password)
        self.enabled = bool(self.host)
        self.debug = _env_bool("SHIP_MQTT_DEBUG", False)
        self.retain_state = _env_bool("SHIP_MQTT_RETAIN_STATE", False)
        self.use_tls = _env_bool("HA_MQTT_TLS", False)
        self.tls_ca_certs = _env_str("HA_MQTT_CA_CERTS", "").strip()
        self.tls_insecure = _env_bool("HA_MQTT_TLS_INSECURE", False)
        self._discovered_object_ids: set[str] = set()
        self._mqtt = None

    def connect(self) -> None:
        """Connect to the MQTT broker (best-effort).

        If MQTT isn't configured (no host), this is a no-op.
        Uses an LWT (will) message so Home Assistant sees us as offline if we crash.
        """
        if not self.enabled:
            return
        try:
            import paho.mqtt.client as mqtt  # type: ignore
        except ModuleNotFoundError:
            _human_print(
                "⚠️  [MQTT] HA_MQTT_HOST gesetzt, aber 'paho-mqtt' fehlt. Installieren mit: pip3 install paho-mqtt"
            )
            self.enabled = False
            return

        # paho-mqtt 2.x deprecates callback API v1; opt into v2 when available.
        client_kwargs: Dict[str, Any] = {"client_id": f"ship-{self.device_id}"}
        cb_api = getattr(mqtt, "CallbackAPIVersion", None)
        if cb_api is not None:
            try:
                client_kwargs["callback_api_version"] = cb_api.VERSION2
            except AttributeError:
                pass

        client = mqtt.Client(**client_kwargs)
        if self.username:
            client.username_pw_set(self.username, self.password or None)
        if self.use_tls:
            client.tls_set(ca_certs=self.tls_ca_certs or None)
            if self.tls_insecure:
                client.tls_insecure_set(True)
        # Ensure HA sees us go offline even on crashes/network loss.
        client.will_set(self._topic_availability(), payload="offline", qos=0, retain=True)
        try:
            client.connect(self.host, self.port, keepalive=30)
            client.loop_start()
            self._mqtt = client
            self.publish_availability(True)
            scheme = "mqtts" if self.use_tls else "mqtt"
            _human_print(f"✅ [MQTT] Connected to {scheme}://{self.host}:{self.port}")
            if self.debug:
                _human_print(
                    "🔎 [MQTT] Debug enabled. "
                    f"device_id={self.device_id} discovery_prefix={self.base_prefix} state_prefix={self.state_prefix} "
                    f"availability_topic={self._topic_availability()}"
                )
        except Exception as e:
            _human_print(f"⚠️  [MQTT] Connect failed: {e}")
            self.enabled = False
            self._mqtt = None

    def close(self) -> None:
        """Publish offline availability and close MQTT cleanly."""
        if not self.enabled or self._mqtt is None:
            return
        self.publish_availability(False)
        try:
            self._mqtt.loop_stop()
            self._mqtt.disconnect()
        except (OSError, RuntimeError):
            pass
        self._mqtt = None

    def _topic_availability(self) -> str:
        return f"{self.state_prefix}/{self.device_id}/availability"

    def publish_availability(self, online: bool) -> None:
        if not self.enabled or self._mqtt is None:
            return
        if self.debug:
            _human_print(f"📤 [MQTT] availability {self._topic_availability()} = {'online' if online else 'offline'} (retain)")
        self._mqtt.publish(
            self._topic_availability(),
            payload=("online" if online else "offline"),
            qos=0,
            retain=True,
        )

    def _topic_state(self, object_id: str) -> str:
        return f"{self.state_prefix}/{self.device_id}/{object_id}/state"

    def _topic_config(self, object_id: str) -> str:
        return f"{self.base_prefix}/sensor/{self.device_id}/{object_id}/config"

    def ensure_discovery(self, *, object_id: str, name: str, unit: str, device_class: str, state_class: str) -> None:
        """Publish Home Assistant MQTT Discovery config for a sensor (once).

        HA listens on `<discovery_prefix>/sensor/<device>/<object_id>/config`.
        Publishing config once is enough; state updates use a separate state topic.
        """
        if not self.enabled or self._mqtt is None:
            return
        if object_id in self._discovered_object_ids:
            return

        payload: Dict[str, Any] = {
            "name": name,
            "unique_id": f"{self.device_id}_{object_id}",
            "state_topic": self._topic_state(object_id),
            "availability_topic": self._topic_availability(),
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": {
                "identifiers": [self.device_id],
                "name": self.device_name,
            },
        }
        if unit:
            payload["unit_of_measurement"] = unit
        if device_class:
            payload["device_class"] = device_class
        if state_class:
            payload["state_class"] = state_class

        if self.debug:
            _human_print(f"📤 [MQTT] discovery {self._topic_config(object_id)} (retain)")
        self._mqtt.publish(self._topic_config(object_id), json.dumps(payload, ensure_ascii=False), qos=0, retain=True)
        self._discovered_object_ids.add(object_id)

    def publish_state(self, *, object_id: str, value: Any) -> None:
        """Publish the current sensor value to the MQTT state topic."""
        if not self.enabled or self._mqtt is None:
            return
        if isinstance(value, bool):
            payload = "true" if value else "false"
        elif isinstance(value, (int, float, str)):
            payload = str(value)
        else:
            payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if self.debug:
            _human_print(f"📤 [MQTT] state {self._topic_state(object_id)} = {payload}")
        self._mqtt.publish(self._topic_state(object_id), payload=payload, qos=0, retain=self.retain_state)


def json_into_eebus_json(payload: Any) -> str:
    """Convert standard JSON objects into the EEBUS array-wrapped JSON format.

    This mirrors the behavior of ship-go's JsonIntoEEBUSJson():
    - Objects (dict) become arrays of single-key objects, recursively.
    - Arrays stay arrays.
    - The top-level array wrapper is stripped.
    """

    def _to_eebus(value: Any) -> Any:
        # ship-go uses an ordered map to preserve field order. Python 3.7+
        # preserves dict insertion order, and we additionally use OrderedDict
        # when parsing from JSON text.
        if isinstance(value, (dict, OrderedDict)):
            return [{k: _to_eebus(v)} for k, v in value.items()]
        if isinstance(value, list):
            return [_to_eebus(v) for v in value]
        return value

    converted = _to_eebus(payload)
    text = json.dumps(converted, separators=(",", ":"), ensure_ascii=False)

    # ship-go trims the first/last bracket (top-level is expected to be an object)
    if text.startswith("[") and text.endswith("]"):
        return text[1:-1]
    return text


# ---------------------------------------------------------------------------
# EEBUS JSON compatibility helpers
# ---------------------------------------------------------------------------
# Many SHIP/SPINE implementations don't send "plain" JSON objects on the wire.
# Instead, they represent objects as a list of single-key objects, recursively.
# The functions below convert between normal JSON and that array-wrapped format.
# ---------------------------------------------------------------------------


def json_text_into_eebus_json(payload_text: str) -> str:
    """Convert a JSON text into EEBUS JSON using ship-go's ordering approach."""
    parsed = json.loads(payload_text, object_pairs_hook=OrderedDict)
    return json_into_eebus_json(parsed)


def json_from_eebus_json(payload_text: str) -> str:
    """Convert array-wrapped EEBUS JSON into standard JSON structurally.

    An encoded object is represented as a list of single-key objects. Real
    arrays remain arrays, and arrays of objects therefore contain nested lists.
    Decoding parsed JSON instead of replacing byte patterns keeps string values
    intact. Empty lists stay lists because an empty encoded object and an empty
    data array are inherently ambiguous without a schema.
    """

    def _from_eebus(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: _from_eebus(item) for key, item in value.items()}
        if not isinstance(value, list):
            return value

        decoded = [_from_eebus(item) for item in value]
        if value and all(isinstance(item, dict) and len(item) == 1 for item in value):
            keys = [next(iter(item)) for item in value]
            if len(keys) == len(set(keys)):
                merged: Dict[str, Any] = {}
                for item in decoded:
                    merged.update(cast(Dict[str, Any], item))
                return merged
        return decoded

    cleaned = payload_text.rstrip("\x00")
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        # The EEBUS top-level object wrapper is stripped on the wire. Objects
        # with more than one key are therefore a comma-separated sequence of
        # single-key JSON objects and need their wrapper restored for parsing.
        if exc.msg != "Extra data":
            raise
        parsed = json.loads(f"[{cleaned}]")
    return json.dumps(_from_eebus(parsed), ensure_ascii=False, separators=(",", ":"))


def _all_cmds(payload_cmd: Any) -> list[Dict[str, Any]]:
    """Return every SPINE command while tolerating array-wrapped nesting."""
    if isinstance(payload_cmd, dict):
        return [payload_cmd]
    if not isinstance(payload_cmd, list):
        return []

    commands: list[Dict[str, Any]] = []
    for item in payload_cmd:
        if isinstance(item, dict):
            commands.append(item)
        elif isinstance(item, list):
            commands.extend(_all_cmds(item))
    return commands


def _parse_spine_datagram(message: Dict[str, Any]) -> Optional[Tuple[Dict[str, Any], list[Dict[str, Any]]]]:
    """Return ``(header, commands)`` from a decoded SHIP data message."""
    data = message.get("data")
    if not isinstance(data, dict):
        return None
    payload = data.get("payload")
    if not isinstance(payload, dict):
        return None
    datagram = payload.get("datagram")
    if not isinstance(datagram, dict):
        return None

    header = datagram.get("header")
    if not isinstance(header, dict):
        return None

    d_payload = datagram.get("payload")
    if not isinstance(d_payload, dict):
        return None

    commands = _all_cmds(d_payload.get("cmd"))
    if not commands:
        return None

    return header, commands


def _make_spine_reply_addresses(
    request_header: Dict[str, Any],
    *,
    local_device_address: str,
) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """Return (address_source, address_destination) for replies/results.

    SPINE replies swap source/destination. Some devices omit the device field
    in addressDestination in requests; we always set our device on addressSource.
    """
    address_destination = request_header.get("addressSource")
    address_source = request_header.get("addressDestination")
    if not isinstance(address_destination, dict) or not isinstance(address_source, dict):
        return None

    address_source = dict(address_source)
    # Important interop detail:
    # Some devices omit the "device" field in addressDestination when addressing the local side.
    # For result/reply messages, mirror the structure the peer used (do not force-inject "device"
    # unless it was present), otherwise some peers treat it as a protocol error.
    if "device" in address_source:
        address_source["device"] = local_device_address

    return address_source, dict(address_destination)


# ---------------------------------------------------------------------------
# Certificate / identity
# ---------------------------------------------------------------------------

def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    """Atomically replace a file and enforce its final permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        os.chmod(path, mode)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


def _certificate_ski(cert: x509.Certificate) -> str:
    extension = cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier)
    return extension.value.digest.hex().lower()


def _normalize_ski(value: str) -> str:
    raw = (value or "").strip().lower()
    if re.search(r"[^0-9a-f:\s-]", raw):
        raise ValueError("Ungültige Zeichen in der SKI")
    normalized = re.sub(r"[:\s-]", "", raw)
    if normalized and len(normalized) != 40:
        raise ValueError(f"Ungültige SKI-Länge: {len(normalized)} (erwartet 40 Hex-Zeichen)")
    return normalized


def _normalize_sha256_fingerprint(value: str) -> str:
    raw = (value or "").strip().lower()
    if re.search(r"[^0-9a-f:\s-]", raw):
        raise ValueError("Ungültige Zeichen im SHA-256-Fingerprint")
    normalized = re.sub(r"[:\s-]", "", raw)
    if normalized and len(normalized) != 64:
        raise ValueError("Ungültige SHA-256-Fingerprint-Länge")
    return normalized


def _certificate_validity(cert: x509.Certificate) -> Tuple[datetime.datetime, datetime.datetime]:
    """Return timezone-aware certificate validity without deprecated eager fallbacks."""
    if hasattr(cert, "not_valid_before_utc"):
        not_before = cert.not_valid_before_utc
        not_after = cert.not_valid_after_utc
    else:
        not_before = cert.not_valid_before.replace(tzinfo=datetime.timezone.utc)
        not_after = cert.not_valid_after.replace(tzinfo=datetime.timezone.utc)
    return not_before, not_after


def get_or_create_certificate() -> str:
    """Create or reuse a local client certificate and return its SKI (hex).

    Why this matters:
    - The Vaillant trust/pairing flow is tied to the client certificate identity.
    - Reusing the same cert keeps the SKI stable across runs.

    Files created/used:
    - cert.pem / key.pem in the current working directory.
    """
    cert_file = Path(_env_str("SHIP_CERT_FILE", "cert.pem"))
    key_file = Path(_env_str("SHIP_KEY_FILE", "key.pem"))
    cert_exists = cert_file.exists()
    key_exists = key_file.exists()
    if cert_exists != key_exists:
        raise RuntimeError("Zertifikat und Private Key müssen immer gemeinsam vorhanden sein")

    if cert_exists and key_exists:
        cert = x509.load_pem_x509_certificate(cert_file.read_bytes())
        key = serialization.load_pem_private_key(key_file.read_bytes(), password=None)
        cert_public = cert.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        key_public = key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        if cert_public != key_public:
            raise RuntimeError("Zertifikat und Private Key gehören nicht zusammen")
        now = datetime.datetime.now(datetime.timezone.utc)
        not_before, not_after = _certificate_validity(cert)
        if now < not_before or now >= not_after:
            raise RuntimeError("Das EEBUS-Zertifikat ist noch nicht oder nicht mehr gültig; Identität nicht automatisch ersetzen")
        os.chmod(key_file, 0o600)
        os.chmod(cert_file, 0o644)
        ski = _certificate_ski(cert)
        _human_print(f"🔄 Zertifikat wiederverwendet (SKI: {ski})")
        return ski

    key = ec.generate_private_key(ec.SECP256R1())
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "EEBUS-Python-Client")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    key_bytes = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    _atomic_write(key_file, key_bytes, 0o600)
    _atomic_write(cert_file, cert.public_bytes(serialization.Encoding.PEM), 0o644)
    ski = _certificate_ski(cert)
    _human_print(f"📜 Neues Zertifikat erstellt (SKI: {ski})")
    return ski


def _load_peer_pin(path: Path) -> Dict[str, str]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise PeerIdentityError("Ungültiges VR921 Peer-Pin-Format")
    ski = _normalize_ski(str(raw.get("ski") or ""))
    try:
        fingerprint = _normalize_sha256_fingerprint(str(raw.get("certificate_sha256") or ""))
    except ValueError as exc:
        raise PeerIdentityError("VR921 Peer-Pin enthält einen ungültigen SHA-256-Fingerprint") from exc
    if not ski:
        raise PeerIdentityError("VR921 Peer-Pin enthält keine SKI")
    return {"ski": ski, "certificate_sha256": fingerprint}


def _store_peer_pin(path: Path, *, ski: str, fingerprint: str) -> None:
    payload = {
        "ski": _normalize_ski(ski),
        "certificate_sha256": _normalize_sha256_fingerprint(fingerprint),
        "pinned_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    _atomic_write(
        path,
        (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        0o600,
    )


def _verify_peer_certificate(
    ws: Any,
    *,
    advertised_ski: str,
    pinned: Dict[str, str],
) -> Tuple[str, str]:
    ssl_object = ws.transport.get_extra_info("ssl_object")
    peer_der = ssl_object.getpeercert(binary_form=True) if ssl_object is not None else None
    if not peer_der:
        raise PeerIdentityError("TLS-Verbindung liefert kein Peer-Zertifikat")
    cert = x509.load_der_x509_certificate(peer_der)
    not_before, not_after = _certificate_validity(cert)
    now = datetime.datetime.now(datetime.timezone.utc)
    if now < not_before or now >= not_after:
        raise PeerIdentityError("Das TLS-Peer-Zertifikat ist noch nicht oder nicht mehr gültig")
    try:
        peer_ski = _certificate_ski(cert)
    except x509.ExtensionNotFound as exc:
        raise PeerIdentityError("Das TLS-Peer-Zertifikat enthält keine SubjectKeyIdentifier-Erweiterung") from exc
    fingerprint = cert.fingerprint(hashes.SHA256()).hex().lower()
    advertised = _normalize_ski(advertised_ski)
    if not advertised or peer_ski != advertised:
        raise PeerIdentityError(
            f"TLS-SKI {peer_ski} stimmt nicht mit mDNS-SKI {advertised or '<leer>'} überein"
        )
    pinned_ski = pinned.get("ski", "")
    if pinned_ski and peer_ski != pinned_ski:
        raise PeerIdentityError(f"TLS-SKI {peer_ski} stimmt nicht mit gespeichertem Peer {pinned_ski} überein")
    pinned_fingerprint = pinned.get("certificate_sha256", "")
    if pinned_fingerprint and fingerprint != pinned_fingerprint:
        raise PeerIdentityError("Das VR921-Zertifikat hat sich trotz gespeicherter Peer-Identität geändert")
    return peer_ski, fingerprint

class MDNSHandler(ServiceListener):
    """Collect a matching `_ship._tcp.local.` peer and track updates/removal.

    The VR921 advertises itself via mDNS. We listen for services and keep the first
    candidate in `target_info`.
    """

    def __init__(
        self,
        ski: str,
        *,
        expected_remote_ski: str = "",
        local_addresses: Iterable[str] = (),
    ):
        self.ski = _normalize_ski(ski)
        self.expected_remote_ski = _normalize_ski(expected_remote_ski)
        self.local_addresses = frozenset(local_addresses)
        self.target_info: Optional[AsyncServiceInfo] = None
        self.target_name: Optional[str] = None
        self._tasks: set[asyncio.Task[Any]] = set()

    def _start_update(self, zc: Any, type_: str, name: str) -> None:
        task = asyncio.create_task(self.async_add_service(zc, type_, name))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def add_service(self, zc, type, name):
        self._start_update(zc, type, name)

    async def async_add_service(self, zc, type, name):
        """Async callback invoked by Zeroconf when a new service appears."""
        info = await zc.async_get_service_info(type, name)
        if info and b"ski" in info.properties:
            try:
                remote_ski = _normalize_ski(info.properties.get(b"ski", b"").decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                remote_ski = ""
            if remote_ski and remote_ski == self.ski:
                return
            if self.local_addresses.intersection(info.parsed_addresses(IPVersion.V4Only)):
                return
            if self.expected_remote_ski and remote_ski != self.expected_remote_ski:
                return
            if remote_ski:
                self.target_info = info
                self.target_name = name

    def remove_service(self, zc, type, name):
        if name == self.target_name:
            self.target_info = None
            self.target_name = None

    def update_service(self, zc, type, name):
        self._start_update(zc, type, name)

async def send_ship_json(ws, data):
    """Sendet SHIP-Control JSON (MessageType=0x01) im EEBUS JSON-Format."""
    # SHIP Control frames are: 0x01 + (EEBUS JSON payload)
    eebus_text = json_into_eebus_json(data)
    msg = b"\x01" + eebus_text.encode("utf-8")
    await ws.send(msg)
    _human_print(f"📤 Gesendet: {list(data.keys())}")


async def send_ship_data(ws, data):
    """Sendet SHIP-Data JSON (MessageType=0x02) im ship-go kompatiblen Format.

    ship-go serialisiert SPINE in zwei Schritten:
    1) SPINE Datagramm -> EEBUS JSON
    2) SHIP Data Envelope (mit placeholder payload) -> EEBUS JSON
       und danach payload wieder als RawMessage hineinpflastern.

    Damit bleibt das SHIP payload-Objekt unverändert (wird nicht nochmal array-wrapped).
    """

    # SHIP Data frames are: 0x02 + (EEBUS JSON payload)
    # The payload is itself a SHIP "data" envelope containing a SPINE datagram.

    # Expect data to be a standard JSON SPINE message like: {"datagram": {...}}
    spine_std = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    spine_eebus = json_text_into_eebus_json(spine_std)

    payload_placeholder = '{"place":"holder"}'
    ship_std_obj = {
        "data": {
            "header": {"protocolId": "ee1.0"},
            "payload": json.loads(payload_placeholder),
        }
    }
    ship_std = json.dumps(ship_std_obj, separators=(",", ":"), ensure_ascii=False)
    ship_eebus = json_text_into_eebus_json(ship_std)

    # ship-go replaces `[payloadPlaceholder]` with the already-transformed SPINE payload
    ship_eebus = ship_eebus.replace(f"[{payload_placeholder}]", spine_eebus)

    msg = b"\x02" + ship_eebus.encode("utf-8")
    await ws.send(msg)


async def send_access_methods(ws, local_ship_id: str):
    """Send the SHIP accessMethods response, which carries our local SHIP id."""
    await send_ship_json(ws, {"accessMethods": {"id": local_ship_id}})


def build_local_detailed_discovery(local_device_address: str) -> Dict[str, Any]:
    """Minimal NodeManagementDetailedDiscoveryData reply.

    Enough to let a remote device identify us and keep the session alive.
    """
    return {
        "specificationVersionList": {"specificationVersion": ["1.3.0"]},
        "deviceInformation": {
            "description": {
                "deviceAddress": {"device": local_device_address},
                "deviceType": "EnergyManagementSystem",
                "featureSet": "smart",
                "brandName": "Python",
                "deviceModel": "SHIP-Layer1",
                "serialNumber": local_device_address,
                "deviceCode": "python-ship",
            }
        },
        "entityInformation": [
            {
                "description": {
                    "entityAddress": {"entity": [0]},
                    "entityType": "DeviceInformation",
                    "description": "DeviceInformation",
                }
            },
            {
                "description": {
                    "entityAddress": {"entity": [1]},
                    "entityType": "CEM",
                    "description": "CEM",
                }
            },
        ],
        "featureInformation": [
            {
                "description": {
                    "featureAddress": {"entity": [0], "feature": 0},
                    "featureType": "NodeManagement",
                    "role": "special",
                    "description": "NodeManagement",
                }
            },
            {
                "description": {
                    "featureAddress": {"entity": [0], "feature": 1},
                    "featureType": "DeviceClassification",
                    "role": "server",
                    "description": "DeviceClassification",
                }
            },
            {
                "description": {
                    "featureAddress": {"entity": [1], "feature": 1},
                    "featureType": "Measurement",
                    "role": "client",
                    "description": "MeasurementClient",
                }
            },
            {
                "description": {
                    "featureAddress": {"entity": [1], "feature": 2},
                    "featureType": "Sensing",
                    "role": "client",
                    "description": "SensingClient",
                }
            },
            *[
                {
                    "description": {
                        "featureAddress": {"entity": list(entity), "feature": feature},
                        "featureType": feature_type,
                        "role": "client",
                        "description": f"{feature_type}Client",
                    }
                }
                for feature_type, (entity, feature) in LOCAL_CLIENT_FEATURES.items()
                if feature_type not in {"Measurement", "Sensing", "NodeManagement"}
            ],
        ],
    }


def build_device_classification_manufacturer_data(local_device_address: str) -> Dict[str, Any]:
    """Minimal DeviceClassificationManufacturerData.

    Keep values simple/consistent; VR921 mostly needs something parseable.
    """
    return {
        "deviceName": "SHIP Python Client",
        "deviceCode": "python-ship",
        "brandName": "Python",
        "powerSource": "mains3Phase",
        "serialNumber": local_device_address,
    }


def build_device_classification_user_data() -> Dict[str, Any]:
    """Minimal DeviceClassificationUserData."""
    return {
        "deviceName": "SHIP Python Client",
    }


def _spine_addr(*, device: str, entity: int | Iterable[int], feature: int) -> Dict[str, Any]:
    """Convenience builder for a SPINE feature address (device/entity/feature)."""
    entity_list = [entity] if isinstance(entity, int) else [int(item) for item in entity]
    return {"device": device, "entity": entity_list, "feature": feature}


# ---------------------------------------------------------------------------
# SPINE send helpers (read/call/result)
# ---------------------------------------------------------------------------


async def send_spine_read(
    ws,
    *,
    address_source: Dict[str, Any],
    address_destination: Dict[str, Any],
    cmd: Dict[str, Any],
    msg_counter: MsgCounter,
    specification_version: str = "1.3.0",
    ack_request: bool = True,
) -> int:
    """Send a SPINE read datagram to the remote device."""
    _human_print(f"📤 [SPINE] Read send: {list(cmd.keys())}")
    counter = await msg_counter.next()
    datagram: Dict[str, Any] = {
        "datagram": {
            "header": {
                "specificationVersion": specification_version,
                "addressSource": address_source,
                "addressDestination": address_destination,
                "msgCounter": counter,
                "cmdClassifier": "read",
                "ackRequest": ack_request,
            },
            "payload": {"cmd": [cmd]},
        }
    }
    await send_ship_data(ws, datagram)
    return counter


async def send_spine_call(
    ws,
    *,
    address_source: Dict[str, Any],
    address_destination: Dict[str, Any],
    cmd: Dict[str, Any],
    msg_counter: MsgCounter,
    specification_version: str = "1.3.0",
    ack_request: bool = True,
) -> int:
    """Send a SPINE call datagram to the remote device."""
    _human_print(f"📤 [SPINE] Call send: {list(cmd.keys())}")
    counter = await msg_counter.next()
    datagram: Dict[str, Any] = {
        "datagram": {
            "header": {
                "specificationVersion": specification_version,
                "addressSource": address_source,
                "addressDestination": address_destination,
                "msgCounter": counter,
                "cmdClassifier": "call",
                "ackRequest": ack_request,
            },
            "payload": {"cmd": [cmd]},
        }
    }
    await send_ship_data(ws, datagram)
    return counter


async def send_spine_result_ok(
    ws,
    *,
    request_header: Dict[str, Any],
    local_device_address: str,
    msg_counter: MsgCounter,
):
    """Send SPINE cmdClassifier='result' (errorNumber 0) acknowledging a received datagram."""
    ref = request_header.get("msgCounter")
    if ref is None:
        return

    addresses = _make_spine_reply_addresses(request_header, local_device_address=local_device_address)
    if addresses is None:
        return

    address_source, address_destination = addresses

    result_datagram: Dict[str, Any] = {
        "datagram": {
            "header": {
                "specificationVersion": request_header.get("specificationVersion", "1.3.0"),
                "addressSource": address_source,
                "addressDestination": address_destination,
                "msgCounter": await msg_counter.next(),
                "msgCounterReference": ref,
                "cmdClassifier": "result",
            },
            "payload": {"cmd": [{"resultData": {"errorNumber": 0}}]},
        }
    }

    await send_ship_data(ws, result_datagram)
    _human_print(f"📤 [SPINE] Result send: msgCounterReference={ref}")


def _entity_addr_list(entity_address: Any) -> Optional[list[int]]:
    if not isinstance(entity_address, dict):
        return None
    entity = entity_address.get("entity")
    if not isinstance(entity, list) or not entity:
        return None
    if not all(isinstance(x, int) for x in entity):
        return None
    return [int(x) for x in entity]



def _extract_feature_capabilities(discovery: Dict[str, Any]) -> list[FeatureCapability]:
    """Preserve every discovered feature address and its advertised operations."""
    capabilities: list[FeatureCapability] = []
    feature_info = discovery.get("featureInformation")
    if not isinstance(feature_info, list):
        return capabilities
    for item in feature_info:
        if not isinstance(item, dict):
            continue
        description = item.get("description")
        if not isinstance(description, dict):
            continue
        address = description.get("featureAddress")
        if not isinstance(address, dict):
            continue
        entity = address.get("entity")
        feature = address.get("feature")
        feature_type = description.get("featureType")
        role = description.get("role")
        if not (
            isinstance(entity, list)
            and entity
            and all(isinstance(part, int) for part in entity)
            and isinstance(feature, int)
            and isinstance(feature_type, str)
            and isinstance(role, str)
        ):
            continue

        operations_by_function: Dict[str, frozenset[str]] = {}
        supported = description.get("supportedFunction")
        if isinstance(supported, list):
            for function_info in supported:
                if not isinstance(function_info, dict):
                    continue
                function = function_info.get("function")
                possible = function_info.get("possibleOperations")
                if not isinstance(function, str):
                    continue
                operations = frozenset(str(name) for name in possible) if isinstance(possible, dict) else frozenset()
                operations_by_function[function] = operations

        capabilities.append(
            FeatureCapability(
                entity=tuple(int(part) for part in entity),
                feature=int(feature),
                feature_type=feature_type,
                role=role,
                operations_by_function=operations_by_function,
            )
        )
    capabilities.sort(key=lambda capability: (capability.entity, capability.feature))
    return capabilities



async def reply_node_management_detailed_discovery(
    ws,
    *,
    request_header: Dict[str, Any],
    local_device_address: str,
    msg_counter: MsgCounter,
):
    """Reply to a SPINE read request for nodeManagementDetailedDiscoveryData."""
    ref = request_header.get("msgCounter")
    if ref is None:
        _human_print("⚠️  [SPINE] Kein msgCounter im Request-Header → kann nicht antworten")
        return

    addresses = _make_spine_reply_addresses(request_header, local_device_address=local_device_address)
    if addresses is None:
        _human_print("⚠️  [SPINE] Request ohne addressSource/addressDestination → kann nicht antworten")
        return

    address_source, address_destination = addresses

    reply_datagram: Dict[str, Any] = {
        "datagram": {
            "header": {
                "specificationVersion": request_header.get("specificationVersion", "1.3.0"),
                "addressSource": address_source,
                "addressDestination": address_destination,
                "msgCounter": await msg_counter.next(),
                "msgCounterReference": ref,
                "cmdClassifier": "reply",
            },
            "payload": {
                "cmd": [
                    {
                        "nodeManagementDetailedDiscoveryData": build_local_detailed_discovery(local_device_address),
                        "function": "nodeManagementDetailedDiscoveryData",
                    }
                ]
            },
        }
    }

    await send_ship_data(ws, reply_datagram)
    _human_print("📤 [SPINE] Reply gesendet: nodeManagementDetailedDiscoveryData")


async def reply_device_classification_manufacturer_data(
    ws,
    *,
    request_header: Dict[str, Any],
    local_device_address: str,
    msg_counter: MsgCounter,
):
    ref = request_header.get("msgCounter")
    if ref is None:
        return

    addresses = _make_spine_reply_addresses(request_header, local_device_address=local_device_address)
    if addresses is None:
        return
    address_source, address_destination = addresses

    reply_datagram: Dict[str, Any] = {
        "datagram": {
            "header": {
                "specificationVersion": request_header.get("specificationVersion", "1.3.0"),
                "addressSource": address_source,
                "addressDestination": address_destination,
                "msgCounter": await msg_counter.next(),
                "msgCounterReference": ref,
                "cmdClassifier": "reply",
            },
            "payload": {
                "cmd": [
                    {
                        "deviceClassificationManufacturerData": build_device_classification_manufacturer_data(
                            local_device_address
                        )
                    }
                ]
            },
        }
    }

    await send_ship_data(ws, reply_datagram)
    _human_print("📤 [SPINE] Reply gesendet: deviceClassificationManufacturerData")


async def reply_device_classification_user_data(
    ws,
    *,
    request_header: Dict[str, Any],
    local_device_address: str,
    msg_counter: MsgCounter,
):
    ref = request_header.get("msgCounter")
    if ref is None:
        return

    addresses = _make_spine_reply_addresses(request_header, local_device_address=local_device_address)
    if addresses is None:
        return
    address_source, address_destination = addresses

    reply_datagram: Dict[str, Any] = {
        "datagram": {
            "header": {
                "specificationVersion": request_header.get("specificationVersion", "1.3.0"),
                "addressSource": address_source,
                "addressDestination": address_destination,
                "msgCounter": await msg_counter.next(),
                "msgCounterReference": ref,
                "cmdClassifier": "reply",
            },
            "payload": {"cmd": [{"deviceClassificationUserData": build_device_classification_user_data()}]},
        }
    }

    await send_ship_data(ws, reply_datagram)
    _human_print("📤 [SPINE] Reply gesendet: deviceClassificationUserData")


async def handle_spine_read(
    ws,
    *,
    request_header: Dict[str, Any],
    cmd: Dict[str, Any],
    local_device_address: str,
    msg_counter: MsgCounter,
):
    """Handle SPINE cmdClassifier='read' with minimal required replies."""
    if "nodeManagementDetailedDiscoveryData" in cmd:
        await reply_node_management_detailed_discovery(
            ws,
            request_header=request_header,
            local_device_address=local_device_address,
            msg_counter=msg_counter,
        )
        return

    if "deviceClassificationManufacturerData" in cmd:
        await reply_device_classification_manufacturer_data(
            ws,
            request_header=request_header,
            local_device_address=local_device_address,
            msg_counter=msg_counter,
        )
        return

    if "deviceClassificationUserData" in cmd:
        await reply_device_classification_user_data(
            ws,
            request_header=request_header,
            local_device_address=local_device_address,
            msg_counter=msg_counter,
        )
        return

    _human_print(f"⚠️  [SPINE] Unhandled read cmd keys: {list(cmd.keys())}")


async def request_remote_detailed_discovery(
    ws,
    *,
    local_device_address: str,
    remote_device_address: str,
    msg_counter: MsgCounter,
) -> int:
    src = _spine_addr(device=local_device_address, entity=0, feature=0)
    dst = _spine_addr(device=remote_device_address, entity=0, feature=0)
    counter = await send_spine_read(
        ws,
        address_source=src,
        address_destination=dst,
        cmd={"nodeManagementDetailedDiscoveryData": {}},
        msg_counter=msg_counter,
    )
    _human_print("📤 [SPINE] Read gesendet: nodeManagementDetailedDiscoveryData")
    return counter


async def request_remote_node_management_use_case_data(
    ws,
    *,
    local_device_address: str,
    remote_device_address: str,
    msg_counter: MsgCounter,
) -> int:
    # Per user requirement: NodeManagement (Entity 0, Feature 0), function nodeManagementUseCaseData
    src = _spine_addr(device=local_device_address, entity=0, feature=0)
    dst = _spine_addr(device=remote_device_address, entity=0, feature=0)
    counter = await send_spine_read(
        ws,
        address_source=src,
        address_destination=dst,
        cmd={"nodeManagementUseCaseData": {}},
        msg_counter=msg_counter,
    )
    _human_print("📤 [SPINE] Read gesendet: nodeManagementUseCaseData")
    return counter



async def request_remote_feature_function(
    ws: Any,
    *,
    local_device_address: str,
    remote_device_address: str,
    capability: FeatureCapability,
    function: str,
    msg_counter: MsgCounter,
) -> int:
    local = LOCAL_CLIENT_FEATURES.get(capability.feature_type)
    if local is None:
        raise ValueError(f"Kein lokales Client-Feature für {capability.feature_type}")
    local_entity, local_feature = local
    source = _spine_addr(device=local_device_address, entity=local_entity, feature=local_feature)
    destination = _spine_addr(
        device=remote_device_address,
        entity=capability.entity,
        feature=capability.feature,
    )
    return await send_spine_read(
        ws,
        address_source=source,
        address_destination=destination,
        cmd={function: {}},
        msg_counter=msg_counter,
        ack_request=True,
    )


async def subscribe_remote_feature(
    ws: Any,
    *,
    local_device_address: str,
    remote_device_address: str,
    capability: FeatureCapability,
    msg_counter: MsgCounter,
) -> Optional[int]:
    local = LOCAL_CLIENT_FEATURES.get(capability.feature_type)
    if local is None or capability.role != "server":
        return None
    local_entity, local_feature = local
    local_client = _spine_addr(
        device=local_device_address,
        entity=local_entity,
        feature=local_feature,
    )
    remote_server = _spine_addr(
        device=remote_device_address,
        entity=capability.entity,
        feature=capability.feature,
    )
    source_nm = _spine_addr(device=local_device_address, entity=0, feature=0)
    destination_nm = _spine_addr(device=remote_device_address, entity=0, feature=0)
    return await send_spine_call(
        ws,
        address_source=source_nm,
        address_destination=destination_nm,
        cmd={
            "nodeManagementSubscriptionRequestCall": {
                "subscriptionRequest": {
                    "clientAddress": local_client,
                    "serverAddress": remote_server,
                    "serverFeatureType": capability.feature_type,
                }
            }
        },
        msg_counter=msg_counter,
        ack_request=True,
    )


def _scaled_number_to_float(value: Any) -> Optional[float]:
    if not isinstance(value, dict):
        return None
    number = value.get("number")
    scale = value.get("scale", 0)
    if not isinstance(number, int) or isinstance(number, bool):
        return None
    if not isinstance(scale, int) or isinstance(scale, bool):
        scale = 0
    if scale < -18 or scale > 18:
        return None
    try:
        return float(number) * (10.0 ** scale)
    except (OverflowError, ValueError):
        return None


def _list_data(value: Any, *candidate_keys: str) -> list[Dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        for key in candidate_keys:
            items = value.get(key)
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
    return []


def parse_measurement_description(cmd: Dict[str, Any]) -> Dict[int, Dict[str, Any]]:
    """Parse measurementDescriptionListData and return {measurementId: {scopeType, unit, measurementType}}.

    Vaillant/VR921 commonly returns this as:
      {"measurementDescriptionListData": [ {measurementId, scopeType, unit, ...}, ... ]}
    (i.e. the value is a list directly).
    """

    desc_map: Dict[int, Dict[str, Any]] = {}

    mdl = cmd.get("measurementDescriptionListData")
    mdl_list: Optional[list] = None

    if isinstance(mdl, list):
        mdl_list = mdl
    elif isinstance(mdl, dict):
        # Fallbacks for alternate nesting seen in other stacks.
        if isinstance(mdl.get("measurementDescriptionListData"), list):
            mdl_list = cast(list, mdl.get("measurementDescriptionListData"))
        elif isinstance(mdl.get("measurementDescriptionData"), list):
            mdl_list = cast(list, mdl.get("measurementDescriptionData"))

    if not isinstance(mdl_list, list):
        _human_print(
            f"⚠️  [MEASUREMENT] measurementDescriptionListData unexpected type: {type(mdl).__name__} "
            f"keys={list(mdl.keys()) if isinstance(mdl, dict) else ''}"
        )
        return desc_map

    for entry in mdl_list:
        if not isinstance(entry, dict):
            continue
        mid = entry.get("measurementId")
        if not isinstance(mid, int):
            continue
        desc_map[mid] = dict(entry)

    return desc_map


def parse_measurement_list(
    cmd: Dict[str, Any],
    desc_map: Dict[int, Dict[str, Any]],
    *,
    source_address: Optional[Dict[str, Any]] = None,
) -> list[Dict[str, Any]]:
    """Parse measurementListData and return structured updates.

    Vaillant/VR921 commonly returns this as:
      {"measurementListData": [ {measurementId, measurementData:{value:{number,scale}}}, ... ]}
    """

    ml = cmd.get("measurementListData")
    ml_list: Optional[list] = None

    if isinstance(ml, list):
        ml_list = ml
    elif isinstance(ml, dict):
        # Fallback for alternate nesting.
        if isinstance(ml.get("measurementListData"), list):
            ml_list = cast(list, ml.get("measurementListData"))
        elif isinstance(ml.get("measurementData"), list):
            ml_list = cast(list, ml.get("measurementData"))

    if not isinstance(ml_list, list):
        _human_print(
            f"⚠️  [MEASUREMENT] measurementListData unexpected type: {type(ml).__name__} "
            f"keys={list(ml.keys()) if isinstance(ml, dict) else ''}"
        )
        return []

    updates: list[Dict[str, Any]] = []
    any_printed = False

    src_entity: Optional[list[int]] = None
    src_feature: Optional[int] = None
    if isinstance(source_address, dict):
        ent = source_address.get("entity")
        feat = source_address.get("feature")
        if isinstance(ent, list) and all(isinstance(x, int) for x in ent):
            src_entity = [int(x) for x in ent]
        if isinstance(feat, int):
            src_feature = int(feat)

    for entry in ml_list:
        if not isinstance(entry, dict):
            continue

        mid = entry.get("measurementId")
        if not isinstance(mid, int):
            continue

        nested_data = entry.get("measurementData")
        value_data = nested_data if isinstance(nested_data, dict) else entry
        val = _scaled_number_to_float(value_data.get("value"))
        if val is None:
            continue

        meta = desc_map.get(mid, {})
        scope = meta.get("scopeType") or "unknown"
        unit = meta.get("unit") or ""
        mtype = meta.get("measurementType") or ""

        # unit can be string/enum-like; keep it printable
        if isinstance(unit, dict):
            unit = unit.get("unit") or unit.get("name") or json.dumps(unit, separators=(",", ":"), ensure_ascii=False)

        scope_str = scope if isinstance(scope, str) else str(scope)
        unit_str = unit if isinstance(unit, str) else str(unit)
        mtype_str = mtype if isinstance(mtype, str) else str(mtype)

        updates.append(
            {
                "measurementId": mid,
                "scopeType": scope_str,
                "unit": unit_str,
                "measurementType": mtype_str,
                "value": val,
                "valueType": value_data.get("valueType"),
                "timestamp": value_data.get("timestamp"),
                "evaluationPeriod": value_data.get("evaluationPeriod"),
                "valueSource": value_data.get("valueSource"),
                "valueTendency": value_data.get("valueTendency"),
                "valueState": value_data.get("valueState"),
                "commodityType": meta.get("commodityType"),
                "calibrationValue": meta.get("calibrationValue"),
                "label": meta.get("label"),
                "description": meta.get("description"),
                "source": {"entity": src_entity, "feature": src_feature},
            }
        )

        # Keep human output helpful but short
        if isinstance(scope_str, str) and (
            "outdoortemperature" in scope_str.lower() or "outsideairtemperature" in scope_str.lower()
        ):
            _human_print(f"🌡️  Außentemperatur: {val} {unit_str} (ID {mid})")
            any_printed = True
        elif isinstance(scope_str, str) and ("dhwtemperature" in scope_str.lower()):
            _human_print(f"🚿 DHW: {val} {unit_str} (ID {mid})")
            any_printed = True
        elif isinstance(scope_str, str) and (
            "acpowertotal" in scope_str.lower() or "power" == scope_str.lower() or "power" in scope_str.lower()
        ):
            _human_print(f"⚡ Leistung: {val} {unit_str} (ID {mid}, scope={scope_str})")
            any_printed = True
        else:
            _human_print(f"📊 Measurement: {val} {unit_str} (scope={scope_str}, ID {mid})")
            any_printed = True

    if not any_printed:
        # If we got here, list existed but no usable values were found.
        sample = json.dumps(ml_list[:3], indent=2, ensure_ascii=False)
        _human_print(f"⚠️  [MEASUREMENT] No values parsed; sample entries:\n{sample}")

    return updates


def parse_setpoint_description(cmd: Dict[str, Any]) -> Dict[int, Dict[str, Any]]:
    descriptions = _list_data(
        cmd.get("setpointDescriptionListData"),
        "setpointDescriptionListData",
        "setpointDescriptionData",
    )
    result: Dict[int, Dict[str, Any]] = {}
    for entry in descriptions:
        setpoint_id = entry.get("setpointId")
        if isinstance(setpoint_id, int) and not isinstance(setpoint_id, bool):
            result[setpoint_id] = dict(entry)
    return result


def parse_setpoint_list(
    cmd: Dict[str, Any],
    desc_map: Dict[int, Dict[str, Any]],
    *,
    source_address: Optional[Dict[str, Any]] = None,
) -> list[Dict[str, Any]]:
    entries = _list_data(cmd.get("setpointListData"), "setpointListData", "setpointData")
    source_entity = _entity_addr_list(source_address) if isinstance(source_address, dict) else None
    source_feature = source_address.get("feature") if isinstance(source_address, dict) else None
    updates: list[Dict[str, Any]] = []
    for entry in entries:
        setpoint_id = entry.get("setpointId")
        if not isinstance(setpoint_id, int) or isinstance(setpoint_id, bool):
            continue
        value = _scaled_number_to_float(entry.get("value"))
        metadata = desc_map.get(setpoint_id, {})
        updates.append(
            {
                "setpointId": setpoint_id,
                "value": value,
                "valueMin": _scaled_number_to_float(entry.get("valueMin")),
                "valueMax": _scaled_number_to_float(entry.get("valueMax")),
                "isSetpointChangeable": entry.get("isSetpointChangeable"),
                "isSetpointActive": entry.get("isSetpointActive"),
                "timePeriod": entry.get("timePeriod"),
                "scopeType": metadata.get("scopeType"),
                "setpointType": metadata.get("setpointType"),
                "unit": _unit_to_ha(metadata.get("unit")),
                "measurementId": metadata.get("measurementId"),
                "label": metadata.get("label"),
                "description": metadata.get("description"),
                "source": {"entity": source_entity, "feature": source_feature},
            }
        )
    return updates


def _address_key(address: Any) -> Optional[Tuple[Tuple[int, ...], int]]:
    if not isinstance(address, dict):
        return None
    entity = address.get("entity")
    feature = address.get("feature")
    if not (
        isinstance(entity, list)
        and entity
        and all(isinstance(item, int) and not isinstance(item, bool) for item in entity)
        and isinstance(feature, int)
        and not isinstance(feature, bool)
    ):
        return None
    return tuple(int(item) for item in entity), int(feature)


def _observed_at() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class SpineRuntime:
    """Read-only SPINE session state and discovery-driven data dispatcher."""

    def __init__(
        self,
        ws: Any,
        *,
        local_device_address: str,
        msg_counter: MsgCounter,
        mqtt_pub: HAMqttPublisher,
        publish_jsonl: bool,
        discovery_log: bool,
    ) -> None:
        self.ws = ws
        self.local_device_address = local_device_address
        self.msg_counter = msg_counter
        self.mqtt_pub = mqtt_pub
        self.publish_jsonl = publish_jsonl
        self.discovery_log = discovery_log
        self.allow_all_advertised = _env_bool("SHIP_READ_ALL_ADVERTISED", False)
        self.subscribe_updates = _env_bool("SHIP_SUBSCRIBE_UPDATES", True)
        self.request_timeout = max(5, _env_int("SHIP_REQUEST_TIMEOUT", 30))
        self.read_delay = max(0, _env_int("SHIP_READ_DELAY_MS", 30)) / 1000.0
        self.remote_device_address: Optional[str] = None
        self.discovery_requested = False
        self.discovery_received = False
        self.use_case_requested = False
        self.use_case_received = False
        self.read_plan_started = False
        self.capabilities: list[FeatureCapability] = []
        self.pending: Dict[int, PendingRequest] = {}
        self.function_cache: Dict[Tuple[Tuple[int, ...], int, str], Dict[str, Any]] = {}
        self.measurement_desc_maps: Dict[Tuple[Tuple[int, ...], int], Dict[int, Dict[str, Any]]] = {}
        self.setpoint_desc_maps: Dict[Tuple[Tuple[int, ...], int], Dict[int, Dict[str, Any]]] = {}
        self.ha_published: set[str] = set()

    def _remember_request(
        self,
        counter: int,
        *,
        function: str,
        target: Tuple[Tuple[int, ...], int],
        kind: str = "read",
    ) -> None:
        self.pending[counter] = PendingRequest(
            counter=counter,
            function=function,
            target=target,
            sent_at=time.monotonic(),
            kind=kind,
        )

    def _expire_requests(self) -> None:
        now = time.monotonic()
        expired = [
            counter
            for counter, request in self.pending.items()
            if now - request.sent_at > self.request_timeout
        ]
        for counter in expired:
            request = self.pending.pop(counter)
            if request.function == "nodeManagementUseCaseData":
                # Use-case data enriches diagnostics but must not permanently
                # block safe discovery-driven reads on peers that don't reply.
                self.use_case_received = True
            _human_print(
                f"⚠️  [SPINE] Timeout counter={counter} kind={request.kind} "
                f"function={request.function} target={request.target}"
            )

    def _learn_remote_device(self, header: Dict[str, Any]) -> None:
        if self.remote_device_address is not None:
            return
        source = header.get("addressSource")
        device = source.get("device") if isinstance(source, dict) else None
        if isinstance(device, str) and device:
            self.remote_device_address = device

    async def _ensure_discovery(self) -> None:
        if self.remote_device_address is None or self.discovery_requested:
            return
        self.discovery_requested = True
        counter = await request_remote_detailed_discovery(
            self.ws,
            local_device_address=self.local_device_address,
            remote_device_address=self.remote_device_address,
            msg_counter=self.msg_counter,
        )
        self._remember_request(counter, function="nodeManagementDetailedDiscoveryData", target=((0,), 0))

    def _node_management_supports(self, function: str, operation: str) -> bool:
        return any(
            capability.feature_type == "NodeManagement"
            and operation in capability.operations_by_function.get(function, frozenset())
            for capability in self.capabilities
        )

    async def _request_use_case(self) -> None:
        if self.remote_device_address is None or self.use_case_requested:
            return
        self.use_case_requested = True
        if not self._node_management_supports("nodeManagementUseCaseData", "read"):
            self.use_case_received = True
            return
        counter = await request_remote_node_management_use_case_data(
            self.ws,
            local_device_address=self.local_device_address,
            remote_device_address=self.remote_device_address,
            msg_counter=self.msg_counter,
        )
        self._remember_request(counter, function="nodeManagementUseCaseData", target=((0,), 0))

    async def _start_read_plan(self) -> None:
        if (
            self.read_plan_started
            or not self.discovery_received
            or not self.use_case_received
            or self.remote_device_address is None
        ):
            return
        self.read_plan_started = True
        can_subscribe = self._node_management_supports("nodeManagementSubscriptionRequestCall", "call")
        selected: list[Tuple[FeatureCapability, Tuple[str, ...]]] = []
        for capability in self.capabilities:
            functions = capability.readable_functions(allow_all_advertised=self.allow_all_advertised)
            functions = tuple(
                function
                for function in functions
                if function not in {"nodeManagementDetailedDiscoveryData", "nodeManagementUseCaseData"}
            )
            if functions and capability.role == "server" and capability.feature_type in LOCAL_CLIENT_FEATURES:
                selected.append((capability, functions))

        _human_print(f"📚 [SPINE] Read-only Plan: {sum(len(item[1]) for item in selected)} Funktionen")
        for capability, functions in selected:
            if self.subscribe_updates and can_subscribe and capability.role == "server":
                counter = await subscribe_remote_feature(
                    self.ws,
                    local_device_address=self.local_device_address,
                    remote_device_address=self.remote_device_address,
                    capability=capability,
                    msg_counter=self.msg_counter,
                )
                if counter is not None:
                    self._remember_request(
                        counter,
                        function=f"subscribe:{capability.feature_type}",
                        target=capability.key,
                        kind="subscription",
                    )
                    if self.read_delay:
                        await asyncio.sleep(self.read_delay)
            for function in functions:
                counter = await request_remote_feature_function(
                    self.ws,
                    local_device_address=self.local_device_address,
                    remote_device_address=self.remote_device_address,
                    capability=capability,
                    function=function,
                    msg_counter=self.msg_counter,
                )
                self._remember_request(counter, function=function, target=capability.key)
                if self.read_delay:
                    await asyncio.sleep(self.read_delay)

    def _handle_result(self, header: Dict[str, Any], commands: list[Dict[str, Any]]) -> None:
        reference = header.get("msgCounterReference")
        request = self.pending.get(reference) if isinstance(reference, int) else None
        error_number: Optional[int] = None
        for command in commands:
            result_data = command.get("resultData")
            if isinstance(result_data, dict) and isinstance(result_data.get("errorNumber"), int):
                error_number = int(result_data["errorNumber"])
                break
        if request is None:
            return
        if error_number not in {None, 0}:
            self.pending.pop(reference, None)
            if request.function == "nodeManagementUseCaseData":
                self.use_case_received = True
            _human_print(
                f"❌ [SPINE] Result error={error_number} counter={reference} "
                f"function={request.function} target={request.target}"
            )
        else:
            request.ack_received = True
            # READ requests remain pending until their data reply arrives. A
            # successful result only acknowledges transport/command receipt.
            if request.kind != "read":
                self.pending.pop(reference, None)
            if self.discovery_log:
                _human_print(f"✅ [SPINE] Result counter={reference} function={request.function}")

    def _complete_reply(self, header: Dict[str, Any]) -> None:
        reference = header.get("msgCounterReference")
        if isinstance(reference, int):
            self.pending.pop(reference, None)

    def _emit_function_data(
        self,
        *,
        header: Dict[str, Any],
        function: str,
        value: Any,
        classifier: str,
    ) -> None:
        source = header.get("addressSource")
        key = _address_key(source)
        if key is not None:
            self.function_cache[(key[0], key[1], function)] = {
                "value": value,
                "observed_at": _observed_at(),
                "classifier": classifier,
            }
        if self.publish_jsonl:
            _emit_jsonl(
                {
                    "type": "spine_function",
                    "classifier": classifier,
                    "function": function,
                    "source": source,
                    "observed_at": _observed_at(),
                    "data": value,
                }
            )

    def _publish_measurements(self, header: Dict[str, Any], command: Dict[str, Any]) -> None:
        key = _address_key(header.get("addressSource"))
        descriptions = self.measurement_desc_maps.get(key, {}) if key is not None else {}
        updates = parse_measurement_list(
            command,
            descriptions,
            source_address=header.get("addressSource"),
        )
        for update in updates:
            scope = str(update.get("scopeType") or "unknown")
            unit = _unit_to_ha(update.get("unit"))
            measurement_type = str(update.get("measurementType") or "")
            measurement_id = update.get("measurementId")
            source = update.get("source") if isinstance(update.get("source"), dict) else {}
            entity = source.get("entity") if isinstance(source, dict) else None
            feature = source.get("feature") if isinstance(source, dict) else None
            object_id = _slug(
                f"{scope}_e{'_'.join(str(item) for item in entity) if isinstance(entity, list) else 'na'}_"
                f"f{feature if isinstance(feature, int) else 'na'}_id{measurement_id}"
            )
            event = {
                "type": "measurement",
                "object_id": object_id,
                **update,
                "unit": unit,
                "observed_at": _observed_at(),
            }
            if self.publish_jsonl:
                _emit_jsonl(event)
            if isinstance(update.get("value"), (int, float)):
                metadata = _guess_ha_metadata(scope, unit, measurement_type)
                if object_id not in self.ha_published:
                    self.mqtt_pub.ensure_discovery(
                        object_id=object_id,
                        name=_friendly_sensor_name(scope, source_entity=entity if isinstance(entity, list) else None),
                        unit=metadata.get("unit", unit),
                        device_class=metadata.get("device_class", ""),
                        state_class=metadata.get("state_class", "measurement"),
                    )
                    self.ha_published.add(object_id)
                self.mqtt_pub.publish_state(object_id=object_id, value=update["value"])

    def _publish_setpoints(self, header: Dict[str, Any], command: Dict[str, Any]) -> None:
        key = _address_key(header.get("addressSource"))
        descriptions = self.setpoint_desc_maps.get(key, {}) if key is not None else {}
        updates = parse_setpoint_list(command, descriptions, source_address=header.get("addressSource"))
        for update in updates:
            event = {"type": "setpoint", **update, "observed_at": _observed_at()}
            if self.publish_jsonl:
                _emit_jsonl(event)
            scope = str(update.get("scopeType") or "setpoint")
            source = update.get("source") if isinstance(update.get("source"), dict) else {}
            entity = source.get("entity") if isinstance(source, dict) else None
            feature = source.get("feature") if isinstance(source, dict) else None
            object_id = _slug(
                f"setpoint_{scope}_e{'_'.join(str(item) for item in entity) if isinstance(entity, list) else 'na'}_"
                f"f{feature if isinstance(feature, int) else 'na'}_id{update.get('setpointId')}"
            )
            value = update.get("value")
            if isinstance(value, (int, float)):
                if object_id not in self.ha_published:
                    self.mqtt_pub.ensure_discovery(
                        object_id=object_id,
                        name=f"{_friendly_sensor_name(scope)} Sollwert",
                        unit=str(update.get("unit") or ""),
                        device_class="temperature" if "temperature" in scope.lower() else "",
                        state_class="measurement",
                    )
                    self.ha_published.add(object_id)
                self.mqtt_pub.publish_state(object_id=object_id, value=value)

    async def _handle_data_command(
        self,
        header: Dict[str, Any],
        command: Dict[str, Any],
        *,
        classifier: str,
    ) -> None:
        for function, value in command.items():
            if function in {"function", "filter", "elements", "selectors"}:
                continue
            self._emit_function_data(
                header=header,
                function=function,
                value=value,
                classifier=classifier,
            )
            if function == "nodeManagementDetailedDiscoveryData" and isinstance(value, dict):
                self.capabilities = _extract_feature_capabilities(value)
                self.discovery_received = True
                _human_print(f"✅ [DISCOVERY] {len(self.capabilities)} Features vollständig inventarisiert")
                if self.discovery_log:
                    for capability in self.capabilities:
                        reads = capability.readable_functions(allow_all_advertised=self.allow_all_advertised)
                        _human_print(
                            f"  entity={list(capability.entity)} feature={capability.feature} "
                            f"type={capability.feature_type} role={capability.role} read={list(reads)}"
                        )
                await self._request_use_case()
            elif function == "nodeManagementUseCaseData":
                self.use_case_received = True
                _human_print("✅ [USECASE] nodeManagementUseCaseData erhalten")
            elif function == "measurementDescriptionListData":
                key = _address_key(header.get("addressSource"))
                descriptions = parse_measurement_description(command)
                if key is not None and descriptions:
                    self.measurement_desc_maps[key] = descriptions
                _human_print(f"✅ [MEASUREMENT] {len(descriptions)} Beschreibungen")
            elif function == "measurementListData":
                self._publish_measurements(header, command)
            elif function == "setpointDescriptionListData":
                key = _address_key(header.get("addressSource"))
                descriptions = parse_setpoint_description(command)
                if key is not None and descriptions:
                    self.setpoint_desc_maps[key] = descriptions
                _human_print(f"✅ [SETPOINT] {len(descriptions)} Beschreibungen")
            elif function == "setpointListData":
                self._publish_setpoints(header, command)
            elif function.startswith("hvac"):
                _human_print(f"✅ [HVAC] {function}")
            elif function.startswith("smartEnergyManagementPs"):
                _human_print(f"✅ [SMART-ENERGY] {function}")
            elif function.startswith("deviceDiagnosis"):
                _human_print(f"✅ [DIAGNOSIS] {function}")
            elif function.startswith("electricalConnection"):
                _human_print(f"✅ [ELECTRICAL] {function}")

    async def handle_datagram(self, header: Dict[str, Any], commands: list[Dict[str, Any]]) -> None:
        self._expire_requests()
        self._learn_remote_device(header)
        classifier = str(header.get("cmdClassifier") or "")
        if classifier != "result" and header.get("ackRequest") is True:
            await send_spine_result_ok(
                self.ws,
                request_header=header,
                local_device_address=self.local_device_address,
                msg_counter=self.msg_counter,
            )
        if classifier == "result":
            self._handle_result(header, commands)
        elif classifier == "read":
            for command in commands:
                await handle_spine_read(
                    self.ws,
                    request_header=header,
                    cmd=command,
                    local_device_address=self.local_device_address,
                    msg_counter=self.msg_counter,
                )
        elif classifier in {"reply", "notify"}:
            if classifier == "reply":
                self._complete_reply(header)
            for command in commands:
                await self._handle_data_command(header, command, classifier=classifier)
        else:
            _human_print(f"⚠️  [SPINE] Nicht unterstützter cmdClassifier={classifier}")

        await self._ensure_discovery()
        await self._start_read_plan()


# ---------------------------------------------------------------------------
# SHIP handshake
# ---------------------------------------------------------------------------


async def perform_ship_handshake(ws, local_ship_id: str):
    """
    SHIP Handshake nach offizieller Spezifikation:
    Phase 1: CMI (Connection Mode Init) - Initial-Byte
    Phase 2: Hello - Trust establishment
    Phase 3: Protocol - Version negotiation
    Phase 4: PIN - Authentication (nur "none" unterstützt)
    Phase 5: Access - Access methods exchange
    """
    
    # === PHASE 1: CMI (Connection Mode Init) ===
    init_ack = await ws.recv()
    if isinstance(init_ack, bytes):
        _human_print(f"📥 [CMI] Initial-Bytes empfangen: {init_ack.hex()}")
    else:
        _human_print(f"📥 [CMI] Initial empfangen (nicht-bytes): {init_ack}")
    
    # === PHASE 2: HELLO ===
    _human_print("📤 [HELLO] Sende connectionHello (phase: ready)...")
    await send_ship_json(ws, {"connectionHello": {"phase": "ready", "waiting": 60000}})

    # State machine for the SHIP handshake phases.
    # We deliberately do not "jump ahead" while the peer is still pending (waiting
    # for the user to press Trust in the myVAILLANT app).
    state = "WAITING_HELLO"
    last_pending_hello_sent = 0.0
    
    while True:
        try:
            raw_msg = await ws.recv()
        except Exception as e:
            # websockets raises ConnectionClosedError/OK subclasses
            code = getattr(e, "code", None)
            reason = getattr(e, "reason", None)
            _human_print(f"❌ WebSocket geschlossen während Handshake: code={code} reason={reason} err={e}")
            return False
        if not isinstance(raw_msg, bytes) or len(raw_msg) < 2:
            continue
        
        # SHIP control frames use message type 0x01.
        # During/after handshake we might also see data frames (0x02).
        header = raw_msg[0]
        if header != 0x01:
            # During/after handshake we might also see data messages (0x02)
            _human_print(f"⚠️  Unerwarteter SHIP MessageType: 0x{header:02x} (len={len(raw_msg)})")
            continue

        payload_raw = raw_msg[1:]
        try:
            payload_text = payload_raw.decode("utf-8", errors="strict")
            payload_text = json_from_eebus_json(payload_text)
            msg = json.loads(payload_text)
            if not isinstance(msg, dict):
                _human_print("❌ [SHIP] Control-Payload ist kein Objekt")
                return False
            if _env_bool("SHIP_HANDSHAKE_LOG", False):
                _human_print(f"📥 Empfangen: {json.dumps(msg, indent=2, ensure_ascii=False)}")
            else:
                _human_print(f"📥 SHIP-Control: {list(msg)}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            _human_print(f"⚠️  JSON Decode Error: {exc}")
            continue

        # === PHASE 2: HELLO RESPONSE ===
        if "connectionHello" in msg and state == "WAITING_HELLO":
            hello = msg.get("connectionHello") or {}
            phase = hello.get("phase")
            
            if phase == "pending":
                prolong = hello.get("prolongationRequest")
                waiting_ms = hello.get("waiting")

                _human_print("⏳ [HELLO] STATUS: PENDING - Warte auf Bestätigung in der myVAILLANT App...")
                _human_print("👉 JETZT in der App den Zugriff bestätigen!")

                # Important: while the remote side is still pending (waiting for user trust/pairing),
                # we MUST NOT proceed to protocol/pin/access. To keep the hello phase alive and
                # avoid timeouts, we answer with our own PENDING + waiting.
                if isinstance(waiting_ms, int):
                    _human_print(f"⏳ [HELLO] Remote waiting={waiting_ms}ms prolongationRequest={prolong}")
                else:
                    _human_print(f"⏳ [HELLO] Remote prolongationRequest={prolong}")

                now = time.monotonic()
                if now - last_pending_hello_sent > 5.0:
                    await send_ship_json(ws, {"connectionHello": {"phase": "pending", "waiting": 60000}})
                    last_pending_hello_sent = now
                # Bleibe in WAITING_HELLO State
                
            elif phase == "ready":
                _human_print("✅ [HELLO] Phase abgeschlossen - beide Seiten READY")
                
                # === PHASE 3: PROTOCOL HANDSHAKE ===
                _human_print("📤 [PROTOCOL] Sende messageProtocolHandshake...")
                await send_ship_json(
                    ws,
                    {
                        "messageProtocolHandshake": {
                            "handshakeType": "announceMax",
                            "version": {"major": 1, "minor": 0},
                            "formats": {"format": ["JSON-UTF8"]},
                        }
                    },
                )
                state = "WAITING_PROTOCOL"
                
            elif phase == "aborted":
                _human_print("❌ [HELLO] Verbindung von Wärmepumpe abgelehnt (Aborted).")
                return False

        # === PHASE 3: PROTOCOL RESPONSE ===
        elif "messageProtocolHandshake" in msg and state == "WAITING_PROTOCOL":
            handshake = msg.get("messageProtocolHandshake") or {}

            # ship-go client behavior: remote replies with "select" -> client must send "select" confirmation
            handshake_type = handshake.get("handshakeType")
            if handshake_type != "select":
                _human_print(f"❌ [PROTOCOL] Unerwarteter handshakeType: {handshake_type}")
                return False
            
            # Validiere Protokoll-Version
            version = handshake.get("version", {})
            if version.get("major") != 1:
                _human_print(f"❌ [PROTOCOL] Nicht unterstützte Version: {version}")
                return False
            
            _human_print("✅ [PROTOCOL] Protokoll-Handshake bestätigt (Version 1.0)")
            _human_print("📤 [PROTOCOL] Bestätige Auswahl (select)...")
            await send_ship_json(
                ws,
                {
                    "messageProtocolHandshake": {
                        "handshakeType": "select",
                        "version": {"major": 1, "minor": 0},
                        "formats": {"format": ["JSON-UTF8"]},
                    }
                },
            )
            
            # === PHASE 4: PIN STATE ===
            _human_print("📤 [PIN] Sende connectionPinState (none)...")
            await send_ship_json(ws, {"connectionPinState": {"pinState": "none"}})
            state = "WAITING_PIN"

        elif "messageProtocolHandshakeError" in msg:
            err = (msg.get("messageProtocolHandshakeError") or {}).get("error")
            _human_print(f"❌ [PROTOCOL] messageProtocolHandshakeError empfangen: error={err}")
            return False
            
        # === PHASE 4: PIN RESPONSE (Optional - manche Geräte bestätigen, manche nicht) ===
        elif "connectionPinState" in msg and state == "WAITING_PIN":
            pin_state = (msg.get("connectionPinState") or {}).get("pinState")
            _human_print(f"✅ [PIN] PIN-State bestätigt: {pin_state}")
            if pin_state != "none":
                _human_print("❌ [PIN] Gerät verlangt PIN (oder sendet unerwarteten Zustand). ship-go unterstützt nur 'none'.")
                return False

            # === PHASE 5: ACCESS METHODS ===
            _human_print("📤 [ACCESS] Sende accessMethodsRequest...")
            await send_ship_json(ws, {"accessMethodsRequest": {}})
            state = "WAITING_ACCESS"
            
        # In ship-go ist PIN eine eigene Phase; ohne PIN-State nicht zur Access-Phase springen.

        # === PHASE 5: ACCESS RESPONSE ===
        elif "accessMethodsRequest" in msg and state == "WAITING_ACCESS":
            _human_print("📥 [ACCESS] accessMethodsRequest vom Gerät empfangen → sende accessMethods...")
            await send_access_methods(ws, local_ship_id)
            # Stay in WAITING_ACCESS until we receive accessMethods

        elif "accessMethods" in msg and state == "WAITING_ACCESS":
            remote_id = (msg.get("accessMethods") or {}).get("id", "unknown")
            _human_print(f"✅ [ACCESS] Access Methods empfangen (Remote ID: {remote_id})")
            _human_print("")
            _human_print("="*60)
            _human_print("💎 SHIP HANDSHAKE ERFOLGREICH BEENDET!")
            _human_print("="*60)
            _human_print("")
            return True
        
        elif state == "WAITING_ACCESS" and "connectionPinState" not in msg and "accessMethods" not in msg and "accessMethodsRequest" not in msg:
            # Falls wir im ACCESS-State sind, aber die falsche Nachricht kommt
            _human_print(f"⚠️  [STATE: {state}] Unerwartete Nachricht: {list(msg.keys())}")

def _service_property(info: AsyncServiceInfo, name: str, default: str = "") -> str:
    raw = info.properties.get(name.encode("utf-8"))
    if raw is None:
        return default
    if isinstance(raw, bytes):
        try:
            return raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return default
    return str(raw)


def _local_ipv4_address() -> str:
    """Resolve the IPv4 address used for outbound LAN traffic without sending data."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 9))
        address = sock.getsockname()[0]
    except OSError:
        address = socket.gethostbyname(socket.gethostname())
    finally:
        sock.close()
    if not address or address.startswith("127."):
        raise RuntimeError("Keine nutzbare lokale IPv4-Adresse für SHIP/mDNS gefunden")
    return address


def _target_endpoint(info: AsyncServiceInfo) -> Tuple[str, int, str, str]:
    addresses = info.parsed_addresses(IPVersion.V4Only)
    if not addresses:
        raise RuntimeError("Der entdeckte SHIP-Dienst enthält keine IPv4-Adresse")
    port = int(info.port)
    if port <= 0 or port > 65535:
        raise RuntimeError(f"Ungültiger SHIP-Port: {port}")
    path = _service_property(info, "path", "/ship/").strip() or "/ship/"
    if not path.startswith("/") or "://" in path:
        raise RuntimeError(f"Ungültiger SHIP-Pfad aus mDNS: {path!r}")
    remote_ski = _normalize_ski(_service_property(info, "ski", ""))
    if not remote_ski:
        raise PeerIdentityError("Der entdeckte SHIP-Dienst enthält keine gültige SKI")
    return addresses[0], port, path, remote_ski


async def _wait_for_target(handler: MDNSHandler, *, timeout: int) -> Optional[AsyncServiceInfo]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if handler.target_info is not None:
            return handler.target_info
        await asyncio.sleep(0.25)
    return None


async def _receive_ship_session(
    ws: Any,
    *,
    runtime: SpineRuntime,
    discovery_log: bool,
) -> None:
    message_count = 0
    maintenance_interval = max(1.0, min(10.0, runtime.request_timeout / 2.0))
    while True:
        try:
            data = await asyncio.wait_for(ws.recv(), timeout=maintenance_interval)
        except asyncio.TimeoutError:
            runtime._expire_requests()
            await runtime._start_read_plan()
            continue

        message_count += 1
        if not isinstance(data, bytes) or not data:
            _human_print(f"⚠️  [SHIP] Nachricht #{message_count} ist nicht binär")
            continue

        message_type = data[0]
        if message_type == 0x01:
            try:
                payload_text = data[1:].decode("utf-8", errors="strict")
                message = json.loads(json_from_eebus_json(payload_text))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                _human_print(f"⚠️  [SHIP] Control-Decodefehler #{message_count}: {exc}")
                continue
            if discovery_log:
                _human_print(f"📨 [SHIP] Control #{message_count}: {list(message) if isinstance(message, dict) else type(message).__name__}")
            continue

        if message_type != 0x02:
            _human_print(f"⚠️  [SHIP] Unbekannter MessageType=0x{message_type:02x} len={len(data)}")
            continue

        try:
            payload_text = data[1:].decode("utf-8", errors="strict")
            message = json.loads(json_from_eebus_json(payload_text))
            if not isinstance(message, dict):
                _human_print(f"⚠️  [SPINE] Datagramm #{message_count} ist kein Objekt")
                continue
            parsed = _parse_spine_datagram(message)
            if parsed is None:
                _human_print(f"⚠️  [SPINE] Datagramm #{message_count} ohne auswertbare Commands")
                continue
            header, commands = parsed
            if discovery_log:
                _human_print(
                    f"📨 [SPINE] #{message_count} classifier={header.get('cmdClassifier')} "
                    f"commands={[list(command) for command in commands]}"
                )
            await runtime.handle_datagram(header, commands)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            _human_print(f"⚠️  [SPINE] Decodefehler #{message_count}: {exc}")
        except Exception as exc:
            _human_print(f"❌ [SPINE] Verarbeitungsfehler #{message_count}: {exc}")
            raise


async def _run_ship_session(
    *,
    target: AsyncServiceInfo,
    handler: MDNSHandler,
    local_ship_id: str,
    local_device_address: str,
    mqtt_pub: HAMqttPublisher,
    peer_pin_path: Path,
    pinned_peer: Dict[str, str],
) -> None:
    target_ip, target_port, target_path, advertised_ski = _target_endpoint(target)
    cert_file = _env_str("SHIP_CERT_FILE", "cert.pem")
    key_file = _env_str("SHIP_KEY_FILE", "key.pem")

    ssl_context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
    ssl_context.load_cert_chain(certfile=cert_file, keyfile=key_file)
    # VR921 certificates are self-signed. Trust is established below by exact
    # SKI/fingerprint pinning instead of the public Web PKI.
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    ssl_context.set_ciphers("HIGH:!aNULL:!eNULL:!MD5@SECLEVEL=1")

    import websockets

    uri = f"wss://{target_ip}:{target_port}{target_path}"
    _human_print(f"🔌 Verbinde WebSocket: {uri}")
    connect_options: Dict[str, Any] = {
        "ssl": ssl_context,
        "subprotocols": cast(Any, ["ship"]),
        "open_timeout": 20,
    }
    # websockets 15 added automatic proxy discovery. SHIP is a LAN protocol;
    # never route the gateway connection through an ambient HTTP proxy.
    if "proxy" in inspect.signature(websockets.connect).parameters:
        connect_options["proxy"] = None

    async with websockets.connect(uri, **connect_options) as ws:
        peer_ski, fingerprint = _verify_peer_certificate(
            ws,
            advertised_ski=advertised_ski,
            pinned=pinned_peer,
        )
        _human_print(f"🔐 TLS-Peer bestätigt: SKI={peer_ski} SHA256={fingerprint}")

        await ws.send(b"\x00\x00")
        if not await perform_ship_handshake(ws, local_ship_id):
            raise RuntimeError("SHIP-Handshake fehlgeschlagen")

        if (
            pinned_peer.get("ski") != peer_ski
            or pinned_peer.get("certificate_sha256") != fingerprint
        ):
            _store_peer_pin(peer_pin_path, ski=peer_ski, fingerprint=fingerprint)
            pinned_peer.clear()
            pinned_peer.update({"ski": peer_ski, "certificate_sha256": fingerprint})
            handler.expected_remote_ski = peer_ski
            _human_print(f"🔒 VR921 Peer-Identität gespeichert: {peer_pin_path}")

        publish_jsonl = _env_bool("SHIP_JSONL", False)
        discovery_log = _env_bool("SHIP_DISCOVERY_LOG", True)
        runtime = SpineRuntime(
            ws,
            local_device_address=local_device_address,
            msg_counter=MsgCounter(start=1),
            mqtt_pub=mqtt_pub,
            publish_jsonl=publish_jsonl,
            discovery_log=discovery_log,
        )
        _human_print("🎯 SHIP Layer erfolgreich; starte read-only SPINE Discovery")
        await _receive_ship_session(ws, runtime=runtime, discovery_log=discovery_log)


async def main() -> None:
    """Run the standalone, persistent, read-only VR921 SHIP/SPINE client."""
    _human_print("🚀 EEBUS SHIP Client gestartet")
    local_ski = get_or_create_certificate()
    local_ship_id = f"python-{local_ski[:12]}"
    local_device_address = f"d:_i:1_{local_ship_id}"

    peer_pin_path = Path(_env_str("VR921_PEER_FILE", "vr921_peer.json"))
    pinned_peer = _load_peer_pin(peer_pin_path)
    configured_remote_ski = _normalize_ski(_env_str("VR921_REMOTE_SKI", ""))
    if configured_remote_ski:
        if pinned_peer.get("ski") and pinned_peer["ski"] != configured_remote_ski:
            raise PeerIdentityError("VR921_REMOTE_SKI widerspricht der gespeicherten Peer-Identität")
        pinned_peer.setdefault("ski", configured_remote_ski)

    if not pinned_peer.get("ski"):
        _human_print(
            "⚠️  Noch keine VR921 Peer-Identität gespeichert. "
            "Der erste erfolgreich in der myVAILLANT App bestätigte TLS-Peer wird dauerhaft gepinnt."
        )

    mqtt_pub = HAMqttPublisher(
        device_id=_slug(_env_str("HA_DEVICE_ID", f"eebus_{local_ship_id}")),
        device_name=_env_str("HA_DEVICE_NAME", "EEBUS HeatPump"),
    )
    aiozc: Optional[AsyncZeroconf] = None
    browser: Optional[AsyncServiceBrowser] = None
    service_info: Optional[AsyncServiceInfo] = None

    try:
        local_ip = _local_ipv4_address()
        aiozc = AsyncZeroconf(ip_version=IPVersion.V4Only)
        handler = MDNSHandler(
            local_ski,
            expected_remote_ski=pinned_peer.get("ski", ""),
            local_addresses=(local_ip,),
        )
        local_port = _env_int("SHIP_LOCAL_ADVERTISEMENT_PORT", 54885)
        mdns_service_name, mdns_properties = _main_pairing_mdns_identity(local_ski)
        service_info = AsyncServiceInfo(
            "_ship._tcp.local.",
            f"{mdns_service_name}._ship._tcp.local.",
            addresses=[socket.inet_aton(local_ip)],
            port=local_port,
            properties=mdns_properties,
        )
        await aiozc.async_register_service(service_info)
        browser = AsyncServiceBrowser(aiozc.zeroconf, "_ship._tcp.local.", handler)
        _human_print(f"📢 mDNS aktiv: {local_ip}")

        await asyncio.to_thread(mqtt_pub.connect)

        minimum_backoff = max(1, _env_int("SHIP_RECONNECT_INITIAL_SECONDS", 2))
        maximum_backoff = max(minimum_backoff, _env_int("SHIP_RECONNECT_MAX_SECONDS", 60))
        backoff = minimum_backoff
        discovery_timeout = max(5, _env_int("SHIP_DISCOVERY_TIMEOUT", 30))

        while True:
            target = await _wait_for_target(handler, timeout=discovery_timeout)
            if target is None:
                message = (
                    f"Kein passender SHIP-Peer nach {discovery_timeout}s gefunden"
                    + (f" (erwartete SKI {handler.expected_remote_ski})" if handler.expected_remote_ski else "")
                )
                if _env_bool("SHIP_EXIT_IF_NOT_FOUND", False):
                    raise TimeoutError(message)
                _human_print(f"⚠️  {message}; suche weiter")
                continue

            attempt_started = time.monotonic()
            try:
                await _run_ship_session(
                    target=target,
                    handler=handler,
                    local_ship_id=local_ship_id,
                    local_device_address=local_device_address,
                    mqtt_pub=mqtt_pub,
                    peer_pin_path=peer_pin_path,
                    pinned_peer=pinned_peer,
                )
            except PeerIdentityError:
                raise
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not pinned_peer.get("certificate_sha256"):
                    _human_print(f"❌ Pairing/SHIP fehlgeschlagen: {exc}")
                    return
                duration = time.monotonic() - attempt_started
                if duration >= 60:
                    backoff = minimum_backoff
                _human_print(f"⚠️  SHIP-Session beendet: {exc}; neuer Versuch in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(maximum_backoff, backoff * 2)
    finally:
        await asyncio.to_thread(mqtt_pub.close)
        if browser is not None:
            await browser.async_cancel()
        if aiozc is not None:
            try:
                await aiozc.async_unregister_all_services()
            finally:
                await aiozc.async_close()
        _human_print("👋 Beendet.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        _human_print("\n👋 Abgebrochen durch Benutzer")
