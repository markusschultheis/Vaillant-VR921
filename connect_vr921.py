"""connect_vr921.py

Diagnostic SHIP/SPINE client for Vaillant VR921/EEBUS devices.
"""

import ssl
import socket
import datetime
import asyncio
import logging
import json
import os
import time
import re
import sys
import uuid
from collections import OrderedDict
from typing import Any, Dict, Optional, Tuple, cast
from zeroconf.asyncio import AsyncZeroconf, AsyncServiceInfo, AsyncServiceBrowser
from zeroconf import IPVersion, ServiceListener
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec


logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
logger = logging.getLogger("VR921")

CERT_DIR = "/home/markus/EEBUS"
os.makedirs(CERT_DIR, exist_ok=True)

class MsgCounter:
    def __init__(self, start: int = 1):
        self._value = start
        self._lock = asyncio.Lock()

    async def next(self) -> int:
        async with self._lock:
            value = self._value
            self._value += 1
            return value

def _env_str(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    if v is None: return default
    return str(v)

def _env_int(name: str, default: int) -> int:
    v = os.environ.get(name)
    if v is None: return default
    try: return int(str(v).strip())
    except Exception: return default

def _env_float(name: str, default: float) -> float:
    v = os.environ.get(name)
    if v is None: return default
    try: return float(str(v).strip())
    except Exception: return default

def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None: return default
    s = str(v).strip().lower()
    if s in {"1", "true", "yes", "y", "on"}: return True
    if s in {"0", "false", "no", "n", "off"}: return False
    return default

def _slug(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"[^a-z0-9_]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "unknown"

def _unit_to_ha(unit: Any) -> str:
    if unit is None: return ""
    if isinstance(unit, str):
        u = unit.strip()
        if u == "degC": return "°C"
        if u == "degF": return "°F"
        return u
    if isinstance(unit, dict):
        u = unit.get("unit") or unit.get("name")
        return _unit_to_ha(str(u)) if u is not None else ""
    return str(unit)

def _guess_ha_metadata(scope_type: str, unit: str) -> Dict[str, str]:
    s = (scope_type or "").lower()
    u = (unit or "").strip()
    if "temperature" in s: return {"device_class": "temperature", "state_class": "measurement", "unit": u or "°C"}
    if "power" in s: return {"device_class": "power", "state_class": "measurement", "unit": u or "W"}
    if "energy" in s: return {"device_class": "energy", "state_class": "total_increasing", "unit": u or "Wh"}
    if "current" in s: return {"device_class": "current", "state_class": "measurement", "unit": u or "A"}
    if "voltage" in s: return {"device_class": "voltage", "state_class": "measurement", "unit": u or "V"}
    if "frequency" in s: return {"device_class": "frequency", "state_class": "measurement", "unit": u or "Hz"}
    return {"device_class": "", "state_class": "measurement", "unit": u}

def _is_energy_measurement_meta(meta: Dict[str, Any]) -> bool:
    if not isinstance(meta, dict): return False
    scope = str(meta.get("scopeType") or "").lower()
    mtype = str(meta.get("measurementType") or "").lower()
    return "energy" in scope or "acenergy" in scope or "energy" in mtype

def _is_acpowertotal_measurement_meta(meta: Dict[str, Any]) -> bool:
    if not isinstance(meta, dict): return False
    scope = str(meta.get("scopeType") or "").lower()
    mtype = str(meta.get("measurementType") or "").lower()
    return "acpowertotal" in scope or "acpowertotal" in mtype

def _friendly_sensor_name(
    scope_type: str,
    *,
    source_entity: Optional[list[int]] = None,
    source_label: str = "",
    measurement_id: Optional[int] = None,
) -> str:
    """Create a truthful HA/display name without inventing phase semantics.

    The VR921 exposes repeated acCurrent/acPower/acVoltage descriptions but does
    not identify their electrical phase in measurementDescriptionListData.
    Those sensors therefore keep their Measurement ID in the public name until
    an explicit phase mapping is proven independently.
    """
    s = (scope_type or "").strip()
    low = s.lower()
    label = (source_label or "").strip()

    if low == "outsideairtemperature":
        base = "Outdoor Temperature"
    elif low == "dhwtemperature":
        base = "DHW Temperature"
    elif low == "roomairtemperature":
        base = "Room Temperature"
    elif low == "acenergyconsumed":
        base = "Energy Consumed"
    elif low == "acenergyproduced":
        base = "Energy Produced"
    elif low == "acfrequency":
        base = "AC Frequency"
    elif low == "acpowertotal":
        base = "AC Power Total"
    elif low == "accurrent":
        base = "AC Current ID %s" % measurement_id if measurement_id is not None else "AC Current"
    elif low == "acpower":
        base = "AC Power ID %s" % measurement_id if measurement_id is not None else "AC Power"
    elif low == "acvoltage":
        base = "AC Voltage ID %s" % measurement_id if measurement_id is not None else "AC Voltage"
    else:
        base = s or "Measurement"
        if measurement_id is not None:
            base = "%s ID %s" % (base, measurement_id)

    if label:
        return "%s (%s)" % (base, label)
    if source_entity:
        return "%s (entity=%s)" % (base, source_entity)
    return base


def _measurement_log_icon(measurement_type: str, scope_type: str) -> str:
    kind = (measurement_type or "").strip().lower()
    scope = (scope_type or "").strip().lower()
    if kind == "temperature" or "temperature" in scope:
        return "🌡️"
    if kind == "energy" or "energy" in scope:
        return "🔋"
    if kind in {"power", "current", "voltage", "frequency"} or scope.startswith("ac"):
        return "⚡"
    return "📏"

def _normalize_ski(value):
    """Normalize EEBUS/SHIP SKI to 40 lowercase hex characters."""
    if value is None:
        return ""

    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="ignore")

    value = str(value).strip().lower()

    # EEBUS SKIs may occasionally be formatted with separators
    value = value.replace(":", "")
    value = value.replace("-", "")
    value = value.replace(" ", "")

    # Don't kill discovery because of malformed foreign mDNS entries.
    if len(value) != 40:
        return ""

    try:
        int(value, 16)
    except ValueError:
        return ""

    return value

class PeerIdentityError(RuntimeError):
    """Raised when the TLS peer identity doesn't match the discovered/pinned VR921."""


def _certificate_ski(cert: x509.Certificate) -> str:
    try:
        return cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value.digest.hex().lower()
    except x509.ExtensionNotFound as exc:
        raise PeerIdentityError("TLS peer certificate has no SubjectKeyIdentifier") from exc


def _peer_pin_file() -> str:
    return os.path.join(CERT_DIR, "vr921_peer.json")


def _load_peer_pin() -> Dict[str, str]:
    path = _peer_pin_file()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return {
            "ski": _normalize_ski(data.get("ski")),
            "certificate_sha256": str(data.get("certificate_sha256") or "").lower(),
            "name": str(data.get("name") or ""),
        }
    except Exception as exc:
        logger.warning("⚠️ Could not read saved VR921 peer identity: %s", exc)
        return {}


def _store_peer_pin(*, ski: str, fingerprint: str, name: str) -> None:
    path = _peer_pin_file()
    tmp = path + ".tmp"
    payload = {
        "ski": _normalize_ski(ski),
        "certificate_sha256": str(fingerprint or "").lower(),
        "name": str(name or ""),
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def _advertised_ski(info: Any) -> str:
    if info is None:
        return ""
    props = getattr(info, "properties", {}) or {}
    raw = props.get(b"ski", b"")
    return _normalize_ski(raw)


def _connection_close_code(exc: BaseException) -> Optional[int]:
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return code
    rcvd = getattr(exc, "rcvd", None)
    rcvd_code = getattr(rcvd, "code", None)
    return int(rcvd_code) if isinstance(rcvd_code, int) else None


def _verify_peer_certificate(ws: Any, *, advertised_ski: str, pinned: Dict[str, str]) -> Tuple[str, str]:
    transport = getattr(ws, "transport", None)
    ssl_object = transport.get_extra_info("ssl_object") if transport is not None else None
    peer_der = ssl_object.getpeercert(binary_form=True) if ssl_object is not None else None
    if not peer_der:
        raise PeerIdentityError("TLS connection did not provide a peer certificate")

    cert = x509.load_der_x509_certificate(peer_der)
    peer_ski = _certificate_ski(cert)
    fingerprint = cert.fingerprint(hashes.SHA256()).hex().lower()
    advertised = _normalize_ski(advertised_ski)

    if not advertised:
        raise PeerIdentityError("mDNS announcement has no valid remote SKI")
    if peer_ski != advertised:
        raise PeerIdentityError(
            "TLS peer SKI %s does not match mDNS SKI %s" % (peer_ski, advertised)
        )

    pinned_ski = _normalize_ski(pinned.get("ski")) if pinned else ""
    if pinned_ski and peer_ski != pinned_ski:
        raise PeerIdentityError(
            "VR921 identity changed: TLS SKI %s, pinned SKI %s" % (peer_ski, pinned_ski)
        )

    # EEBUS trust is tied to the key/SKI. A renewed certificate can have a new
    # full-certificate fingerprint while retaining the same trusted public key.
    pinned_fp = str((pinned or {}).get("certificate_sha256") or "").lower()
    if pinned_fp and pinned_fp != fingerprint:
        logger.warning(
            "⚠️ VR921 certificate fingerprint changed while SKI stayed stable; accepting same EEBUS key identity"
        )

    return peer_ski, fingerprint


def json_into_eebus_json(payload):
    #Wandelt normales Python-JSON in das von SHIP/EEBUS
    #verwendete array-wrapped JSON-Format um.

    #Beispiel:
    #    {"connectionHello": {"phase": "ready"}}

    #wird intern entsprechend der EEBUS-Darstellung serialisiert.

    def _to_eebus(value):
        if isinstance(value, dict):
            return [
                {key: _to_eebus(val)}
                for key, val in value.items()
            ]

        if isinstance(value, list):
            return [
                _to_eebus(item)
                for item in value
            ]

        return value

    converted = _to_eebus(payload)

    text = json.dumps(
        converted,
        separators=(",", ":"),
        ensure_ascii=False
    )

    # EEBUS erwartet auf Top-Level kein äußeres Array.
    if text.startswith("[") and text.endswith("]"):
        return text[1:-1]

    return text

def json_text_into_eebus_json(payload_text: str) -> str:
    """
    Wandelt einen JSON-String in das EEBUS-JSON-Format um.
    """
    parsed = json.loads(payload_text)
    return json_into_eebus_json(parsed)


async def send_ship_data(ws, data):

    #Sendet ein SPINE-Datagramm innerhalb eines SHIP-DATA-Frames.

    #SHIP DATA:
    #    0x02
    #    + SHIP data envelope
    #    + SPINE payload

    # 1. SPINE-Datagramm separat in EEBUS JSON umwandeln
    spine_std = json.dumps(
        data,
        separators=(",", ":"),
        ensure_ascii=False
    )

    spine_eebus = json_text_into_eebus_json(spine_std)

    # 2. SHIP DATA Envelope mit Platzhalter erzeugen
    payload_placeholder = '{"place":"holder"}'

    ship_std_obj = {
        "data": {
            "header": {
                "protocolId": "ee1.0"
            },
            "payload": json.loads(payload_placeholder),
        }
    }

    ship_std = json.dumps(
        ship_std_obj,
        separators=(",", ":"),
        ensure_ascii=False
    )

    ship_eebus = json_text_into_eebus_json(ship_std)

    # 3. Platzhalter durch bereits korrekt serialisiertes
    #    SPINE-Datagramm ersetzen.
    #
    # Wichtig:
    # Das SPINE-Payload darf hier NICHT erneut array-wrapped werden.
    ship_eebus = ship_eebus.replace(
        f"[{payload_placeholder}]",
        spine_eebus
    )

    # 4. SHIP DATA message type = 0x02
    msg = b"\x02" + ship_eebus.encode("utf-8")

    await ws.send(msg)

async def send_ship_json(ws, data):
    """
    Sendet ein SHIP-Control-Frame.

    SHIP Control:
        0x01 + EEBUS JSON
    """

    eebus_text = json_into_eebus_json(data)

    msg = (
        b"\x01"
        + eebus_text.encode("utf-8")
    )

    await ws.send(msg)

    logger.info(
        "📤 SHIP Control sent: %s",
        list(data.keys())
    )

class HAMqttPublisher:
    def __init__(self, *, device_id: str, device_name: str):
        self.device_id = device_id
        self.device_name = device_name
        self.base_prefix = _env_str("HA_MQTT_PREFIX", "homeassistant").strip("/")
        self.state_prefix = _env_str("HA_MQTT_STATE_PREFIX", "ship").strip("/")

        secrets_host = ""
        secrets_port = 1883
        secrets_user = ""
        secrets_password = ""
        try:
            import mqtt_secrets as _mqtt_secrets
            secrets_host = str(getattr(_mqtt_secrets, "HA_MQTT_HOST", "") or "").strip()
            secrets_port = int(getattr(_mqtt_secrets, "HA_MQTT_PORT", 1883) or 1883)
            secrets_user = str(getattr(_mqtt_secrets, "HA_MQTT_USER", "") or "").strip()
            secrets_password = str(getattr(_mqtt_secrets, "HA_MQTT_PASSWORD", "") or "")
        except Exception: pass

        self.host = _env_str("HA_MQTT_HOST", secrets_host).strip()
        self.port = _env_int("HA_MQTT_PORT", secrets_port)
        self.username = _env_str("HA_MQTT_USER", secrets_user)
        self.password = _env_str("HA_MQTT_PASSWORD", secrets_password)
        self.enabled = bool(self.host)
        self.retain_state = _env_bool("SHIP_MQTT_RETAIN_STATE", False)
        self._discovered_object_ids: set[str] = set()
        self._mqtt = None

    def connect(self) -> None:
        if not self.enabled: return
        try: import paho.mqtt.client as mqtt
        except Exception:
            print("⚠️  [MQTT] paho-mqtt fehlt.")
            self.enabled = False
            return

        client_kwargs: Dict[str, Any] = {"client_id": f"ship-{self.device_id}"}
        cb_api = getattr(mqtt, "CallbackAPIVersion", None)
        if cb_api is not None:
            try: client_kwargs["callback_api_version"] = cb_api.VERSION2
            except Exception: pass

        client = mqtt.Client(**client_kwargs)
        if self.username: client.username_pw_set(self.username, self.password or None)
        try: client.will_set(self._topic_availability(), payload="offline", qos=0, retain=True)
        except Exception: pass
        try:
            client.connect(self.host, self.port, keepalive=30)
            client.loop_start()
            self._mqtt = client
            self.publish_availability(True)
            print(f"✅ [MQTT] Connected to mqtt://{self.host}:{self.port}")
        except Exception as e:
            print(f"⚠️  [MQTT] Connect failed: {e}")
            self.enabled = False
            self._mqtt = None

    def close(self) -> None:
        if not self.enabled or self._mqtt is None: return
        try: self.publish_availability(False)
        except Exception: pass
        try:
            self._mqtt.loop_stop()
            self._mqtt.disconnect()
        except Exception: pass
        self._mqtt = None

    def _topic_availability(self) -> str: return f"{self.state_prefix}/{self.device_id}/availability"
    def _topic_state(self, object_id: str) -> str: return f"{self.state_prefix}/{self.device_id}/{object_id}/state"
    def _topic_config(self, object_id: str) -> str: return f"{self.base_prefix}/sensor/{self.device_id}/{object_id}/config"

    def publish_availability(self, online: bool) -> None:
        if not self.enabled or self._mqtt is None: return
        self._mqtt.publish(self._topic_availability(), payload=("online" if online else "offline"), qos=0, retain=True)

    def ensure_discovery(self, *, object_id: str, name: str, unit: str, device_class: str, state_class: str) -> None:
        if not self.enabled or self._mqtt is None: return
        if object_id in self._discovered_object_ids: return
        payload: Dict[str, Any] = {
            "name": name,
            "unique_id": f"{self.device_id}_{object_id}",
            "state_topic": self._topic_state(object_id),
            "availability_topic": self._topic_availability(),
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": {"identifiers": [self.device_id], "name": self.device_name},
        }
        if unit: payload["unit_of_measurement"] = unit
        if device_class: payload["device_class"] = device_class
        if state_class: payload["state_class"] = state_class
        self._mqtt.publish(self._topic_config(object_id), json.dumps(payload, ensure_ascii=False), qos=0, retain=True)
        self._discovered_object_ids.add(object_id)

    def publish_state(self, *, object_id: str, value: float) -> None:
        if not self.enabled or self._mqtt is None: return
        self._mqtt.publish(self._topic_state(object_id), payload=str(value), qos=0, retain=self.retain_state)


def json_into_eebus_json(payload: Any) -> str:
    def _to_eebus(value: Any) -> Any:
        if isinstance(value, (dict, OrderedDict)): return [{k: _to_eebus(v)} for k, v in value.items()]
        if isinstance(value, list): return [_to_eebus(v) for v in value]
        return value
    converted = _to_eebus(payload)
    text = json.dumps(converted, separators=(",", ":"), ensure_ascii=False)
    if text.startswith("[") and text.endswith("]"): return text[1:-1]
    return text

def json_text_into_eebus_json(payload_text: str) -> str:
    parsed = json.loads(payload_text, object_pairs_hook=OrderedDict)
    return json_into_eebus_json(parsed)

def json_from_eebus_json(payload_text: str) -> str:
    b = payload_text.encode("utf-8", errors="ignore")
    b = b.replace(b"[{", b"{").replace(b"},{", b",").replace(b"}]", b"}").replace(b"[]", b"{}").strip(b"\x00")
    return b.decode("utf-8", errors="ignore")

def _first_cmd(payload_cmd: Any) -> Optional[Dict[str, Any]]:
    if isinstance(payload_cmd, dict): return payload_cmd
    if not isinstance(payload_cmd, list) or not payload_cmd: return None
    first = payload_cmd[0]
    if isinstance(first, list) and first:
        inner = first[0]
        return inner if isinstance(inner, dict) else None
    return first if isinstance(first, dict) else None

def _parse_spine_datagram(message: Dict[str, Any]) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
    data = message.get("data")
    if not isinstance(data, dict): return None
    payload = data.get("payload")
    if not isinstance(payload, dict): return None
    datagram = payload.get("datagram")
    if not isinstance(datagram, dict): return None
    header = datagram.get("header")
    if not isinstance(header, dict): return None
    d_payload = datagram.get("payload")
    if not isinstance(d_payload, dict): return None
    cmd = _first_cmd(d_payload.get("cmd"))
    if cmd is None: return None
    return header, cmd

def _make_spine_reply_addresses(request_header: Dict[str, Any], *, local_device_address: str) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
    address_destination = request_header.get("addressSource")
    address_source = request_header.get("addressDestination")
    if not isinstance(address_destination, dict) or not isinstance(address_source, dict): return None
    address_source = dict(address_source)
    if "device" in address_source: address_source["device"] = local_device_address
    return address_source, dict(address_destination)

def get_or_create_certificate():
    cert_file = os.path.join(CERT_DIR, "cert.pem")
    key_file = os.path.join(CERT_DIR, "key.pem")
    cert_exists = os.path.exists(cert_file)
    key_exists = os.path.exists(key_file)

    # Never silently replace only half of an existing EEBUS identity. Doing so
    # would create a new SKI and invalidate the trust stored in the VR921.
    if cert_exists != key_exists:
        raise RuntimeError(
            "Incomplete EEBUS identity: cert.pem/key.pem must either both exist or both be absent"
        )

    if cert_exists and key_exists:
        with open(cert_file, "rb") as f:
            cert = x509.load_pem_x509_certificate(f.read())
        with open(key_file, "rb") as f:
            key = serialization.load_pem_private_key(f.read(), password=None)

        cert_pub = cert.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        key_pub = key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        if cert_pub != key_pub:
            raise RuntimeError("EEBUS cert.pem and key.pem do not belong to the same identity")

        ski = _certificate_ski(cert)
        print(f"🔄 Zertifikat wiederverwendet (SKI: {ski})")
        return ski

    key = ec.generate_private_key(ec.SECP256R1())
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, u"EEBUS-Python-Client")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = x509.CertificateBuilder().subject_name(subject).issuer_name(issuer).public_key(
        key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(
        now - datetime.timedelta(days=1)).not_valid_after(
        now + datetime.timedelta(days=3650)
    ).add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False).sign(key, hashes.SHA256())
    with open(cert_file, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_file, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    try:
        os.chmod(cert_file, 0o600)
        os.chmod(key_file, 0o600)
    except OSError:
        pass
    ski = _certificate_ski(cert)
    print(f"📜 Neues Zertifikat erstellt (SKI: {ski})")
    return ski

class MDNSHandler(ServiceListener):
    """
    Findet einen entfernten SHIP/EEBUS-Peer.

    Wichtig:
    - behält target_info für Kompatibilität mit dem bestehenden main()
    - ignoriert die eigene SKI
    - ignoriert ALLE SHIP-Dienste auf der eigenen IP
    """

    def __init__(self, ski, local_ip):
        self.ski = _normalize_ski(ski)
        self.local_ip = local_ip
        self.target_info = None
        self.target_name = None

    def add_service(self, zc, type_, name):
        asyncio.ensure_future(
            self.async_add_service(zc, type_, name)
        )

    def update_service(self, zc, type_, name):
        asyncio.ensure_future(
            self.async_add_service(zc, type_, name)
        )

    def remove_service(self, zc, type_, name):
        if name == self.target_name:
            print(f"↩️  SHIP-Dienst entfernt: {name}")
            self.target_info = None
            self.target_name = None

    async def async_add_service(self, zc, type_, name):
        try:
            info = await zc.async_get_service_info(type_, name)

            if info is None:
                return

            # IPv4-Adressen des Dienstes bestimmen
            try:
                addresses = info.parsed_addresses(IPVersion.V4Only)
            except Exception:
                addresses = []

            if not addresses:
                return

            # ------------------------------------------------------
            # 1. Alle SHIP-Dienste auf dem eigenen Rechner ignorieren
            # ------------------------------------------------------
            if self.local_ip in addresses:
                print(
                    f"↩️  Ignoriere lokalen SHIP-Dienst: "
                    f"{name} ({self.local_ip})"
                )
                return

            # ------------------------------------------------------
            # 2. Remote-SKI lesen
            # ------------------------------------------------------
            raw_ski = info.properties.get(b"ski", b"")

            try:
                if isinstance(raw_ski, bytes):
                    raw_ski = raw_ski.decode("utf-8", errors="ignore")

                remote_ski = _normalize_ski(raw_ski)
            except Exception:
                remote_ski = ""

            if not remote_ski:
                print(
                    f"↩️  Ignoriere SHIP-Dienst ohne gültige SKI: {name}"
                )
                return

            # ------------------------------------------------------
            # 3. Eigene Zertifikats-SKI ignorieren
            # ------------------------------------------------------
            if remote_ski == self.ski:
                print(
                    f"↩️  Ignoriere eigene SHIP-Identität: {name}"
                )
                return

            # ------------------------------------------------------
            # 4. Für Vaillant-Client nur VR921 akzeptieren
            # ------------------------------------------------------
            if not name.lower().startswith("vr921"):
                print(
                    f"↩️  Ignoriere Nicht-VR921 SHIP-Dienst: "
                    f"{name} ({addresses[0]})"
                )
                return

            # ------------------------------------------------------
            # 5. VR921 gefunden
            # ------------------------------------------------------
            print(
                f"🔎 VR921-Kandidat: "
                f"{name} -> {addresses[0]}:{info.port} "
                f"SKI={remote_ski}"
            )

            # Bestehendes main() erwartet target_info
            if self.target_info is None:
                self.target_info = info
                self.target_name = name

        except Exception as exc:
            print(
                f"⚠️  Fehler bei mDNS-Auswertung von {name}: {exc}"
            )
def build_local_detailed_discovery(local_device_address: str) -> Dict[str, Any]:
    return {
        "specificationVersionList": {"specificationVersion": ["1.3.0"]},
        "deviceInformation": {"description": {"deviceAddress": {"device": local_device_address}, "deviceType": "EnergyManagementSystem", "featureSet": "smart", "brandName": "Python", "deviceModel": "SHIP-Layer1", "serialNumber": local_device_address, "deviceCode": "python-ship"}},
        "entityInformation": [
            {"description": {"entityAddress": {"entity": [0]}, "entityType": "DeviceInformation", "description": "DeviceInformation"}},
            {"description": {"entityAddress": {"entity": [1]}, "entityType": "CEM", "description": "CEM"}},
        ],
        "featureInformation": [
            {"description": {"featureAddress": {"entity": [0], "feature": 0}, "featureType": "NodeManagement", "role": "special", "description": "NodeManagement"}},
            {"description": {"featureAddress": {"entity": [0], "feature": 1}, "featureType": "DeviceClassification", "role": "server", "description": "DeviceClassification"}},
            {"description": {"featureAddress": {"entity": [1], "feature": 1}, "featureType": "Measurement", "role": "client", "description": "MeasurementClient"}},
            {"description": {"featureAddress": {"entity": [1], "feature": 2}, "featureType": "Sensing", "role": "client", "description": "SensingClient"}},
        ],
    }

def build_device_classification_manufacturer_data(local_device_address: str) -> Dict[str, Any]:
    return {"deviceName": "SHIP Python Client", "deviceCode": "python-ship", "brandName": "Python", "powerSource": "mains3Phase", "serialNumber": local_device_address}

def build_device_classification_user_data() -> Dict[str, Any]:
    return {"deviceName": "SHIP Python Client"}

def _spine_addr(*, device: str, entity: int, feature: int) -> Dict[str, Any]:
    return {"device": device, "entity": [entity], "feature": feature}

async def send_spine_read(ws, *, address_source: Dict[str, Any], address_destination: Dict[str, Any], cmd: Dict[str, Any], msg_counter: MsgCounter, specification_version: str = "1.3.0", ack_request: bool = True) -> int:
    counter = await msg_counter.next()
    datagram: Dict[str, Any] = {
        "datagram": {
            "header": {"specificationVersion": specification_version, "addressSource": address_source, "addressDestination": address_destination, "msgCounter": counter, "cmdClassifier": "read", "ackRequest": ack_request},
            "payload": {"cmd": [cmd]},
        }
    }
    await send_ship_data(ws, datagram)
    return counter

async def send_spine_call(ws, *, address_source: Dict[str, Any], address_destination: Dict[str, Any], cmd: Dict[str, Any], msg_counter: MsgCounter, specification_version: str = "1.3.0", ack_request: bool = True):
    datagram: Dict[str, Any] = {
        "datagram": {
            "header": {"specificationVersion": specification_version, "addressSource": address_source, "addressDestination": address_destination, "msgCounter": await msg_counter.next(), "cmdClassifier": "call", "ackRequest": ack_request},
            "payload": {"cmd": [cmd]},
        }
    }
    await send_ship_data(ws, datagram)

async def send_spine_result_ok(ws, *, request_header: Dict[str, Any], local_device_address: str, msg_counter: MsgCounter):
    ref = request_header.get("msgCounter")
    if ref is None: return
    addresses = _make_spine_reply_addresses(request_header, local_device_address=local_device_address)
    if addresses is None: return
    address_source, address_destination = addresses
    result_datagram: Dict[str, Any] = {
        "datagram": {
            "header": {"specificationVersion": request_header.get("specificationVersion", "1.3.0"), "addressSource": address_source, "addressDestination": address_destination, "msgCounter": await msg_counter.next(), "msgCounterReference": ref, "cmdClassifier": "result"},
            "payload": {"cmd": [{"resultData": {"errorNumber": 0}}]},
        }
    }
    await send_ship_data(ws, result_datagram)

def _extract_remote_landmap(discovery: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    heat_pump_entity_addr: Optional[Dict[str, Any]] = None
    feature_type_to_addr: Dict[str, Dict[str, Any]] = {}
    entity_info = discovery.get("entityInformation")
    if isinstance(entity_info, list):
        for item in entity_info:
            if not isinstance(item, dict): continue
            desc = item.get("description")
            if not isinstance(desc, dict): continue
            if desc.get("entityType") == "HeatPumpAppliance":
                ent_addr = desc.get("entityAddress")
                if isinstance(ent_addr, dict):
                    heat_pump_entity_addr = ent_addr
                    break
    feature_info = discovery.get("featureInformation")
    if isinstance(feature_info, list):
        for item in feature_info:
            if not isinstance(item, dict): continue
            desc = item.get("description")
            if not isinstance(desc, dict): continue
            feature_type = desc.get("featureType")
            feature_addr = desc.get("featureAddress")
            if isinstance(feature_type, str) and isinstance(feature_addr, dict):
                feature_type_to_addr[feature_type] = feature_addr
    return heat_pump_entity_addr, feature_type_to_addr

def _extract_entity_labels(discovery: Dict[str, Any]) -> Dict[Tuple[int, ...], str]:
    labels: Dict[Tuple[int, ...], str] = {}
    entity_info = discovery.get("entityInformation")
    if not isinstance(entity_info, list):
        return labels
    for item in entity_info:
        if not isinstance(item, dict):
            continue
        desc = item.get("description")
        if not isinstance(desc, dict):
            continue
        addr = desc.get("entityAddress")
        entity = addr.get("entity") if isinstance(addr, dict) else None
        if not isinstance(entity, list) or not entity or not all(isinstance(x, int) for x in entity):
            continue
        label = desc.get("description") or desc.get("entityType")
        if isinstance(label, str) and label.strip():
            labels[tuple(int(x) for x in entity)] = label.strip()
    return labels


def _extract_measurement_servers(discovery: Dict[str, Any]) -> list[Dict[str, Any]]:
    all_servers: list[Dict[str, Any]] = []
    entity_one_servers: list[Dict[str, Any]] = []
    feature_info = discovery.get("featureInformation")
    if not isinstance(feature_info, list): return []
    for item in feature_info:
        if not isinstance(item, dict): continue
        desc = item.get("description")
        if not isinstance(desc, dict): continue
        if desc.get("role") != "server" or desc.get("featureType") != "Measurement": continue
        faddr = desc.get("featureAddress")
        if not isinstance(faddr, dict): continue
        entity = faddr.get("entity")
        feature = faddr.get("feature")
        if not isinstance(entity, list) or not entity or not all(isinstance(x, int) for x in entity): continue
        if not isinstance(feature, int): continue
        server = {"entity": [int(x) for x in entity], "feature": int(feature)}
        all_servers.append(server)
        if entity == [1]:
            entity_one_servers.append(server)
    if entity_one_servers:
        logger.info("📌 Found %d measurement server(s) on entity [1]", len(entity_one_servers))
        return entity_one_servers
    if all_servers:
        logger.warning("⚠️ No measurement servers on entity [1]; falling back to %d available measurement server(s)", len(all_servers))
        return all_servers
    logger.warning("⚠️ No measurement servers found in discovery response")
    return []

async def request_remote_detailed_discovery(ws, *, local_device_address: str, remote_device_address: str, msg_counter: MsgCounter):
    src = _spine_addr(device=local_device_address, entity=0, feature=0)
    dst = _spine_addr(device=remote_device_address, entity=0, feature=0)
    await send_spine_read(ws, address_source=src, address_destination=dst, cmd={"nodeManagementDetailedDiscoveryData": {}}, msg_counter=msg_counter)

async def request_remote_node_management_use_case_data(ws, *, local_device_address: str, remote_device_address: str, msg_counter: MsgCounter):
    src = _spine_addr(device=local_device_address, entity=0, feature=0)
    dst = _spine_addr(device=remote_device_address, entity=0, feature=0)
    await send_spine_read(ws, address_source=src, address_destination=dst, cmd={"nodeManagementUseCaseData": {}}, msg_counter=msg_counter)

async def request_remote_measurement_once(ws, *, local_device_address: str, remote_device_address: str, remote_measurement_feature: Dict[str, Any], msg_counter: MsgCounter):
    entity_list = remote_measurement_feature.get("entity")
    feature = remote_measurement_feature.get("feature")
    if not isinstance(entity_list, list) or not entity_list or not all(isinstance(x, int) for x in entity_list): return
    if not isinstance(feature, int): return
    logger.info("📤 Polling measurement once for server entity=%s feature=%s", entity_list, feature)
    src = _spine_addr(device=local_device_address, entity=1, feature=1)
    dst = {"device": remote_device_address, "entity": [int(x) for x in entity_list], "feature": int(feature)}
    await send_spine_read(ws, address_source=src, address_destination=dst, cmd={"measurementDescriptionListData": {}}, msg_counter=msg_counter, ack_request=True)

    # Compatibility/default-value probe. The VR921 compressor feature has been
    # observed to return only its preferred/default value (acPowerTotal) when
    # measurementListData is read without a selector.
    await send_spine_read(ws, address_source=src, address_destination=dst, cmd={"measurementListData": {}}, msg_counter=msg_counter, ack_request=True)


async def request_remote_measurement_id(
    ws,
    *,
    local_device_address: str,
    remote_device_address: str,
    remote_measurement_feature: Dict[str, Any],
    measurement_id: int,
    msg_counter: MsgCounter,
) -> Optional[int]:
    """Read exactly one advertised Measurement entry via its SPINE selector."""
    entity_list = remote_measurement_feature.get("entity")
    feature = remote_measurement_feature.get("feature")
    if not isinstance(entity_list, list) or not entity_list or not all(isinstance(x, int) for x in entity_list):
        return
    if not isinstance(feature, int) or not isinstance(measurement_id, int) or measurement_id < 0:
        return

    src = _spine_addr(device=local_device_address, entity=1, feature=1)
    dst = {
        "device": remote_device_address,
        "entity": [int(x) for x in entity_list],
        "feature": int(feature),
    }

    # SPINE CmdType carries Function + Filter as siblings of the data function.
    # When a filter is present, Function must identify the filtered list function.
    cmd = {
        "function": "measurementListData",
        "filter": [
            {
                "cmdControl": {"partial": {}},
                "measurementListDataSelectors": {
                    "measurementId": int(measurement_id),
                },
            }
        ],
        "measurementListData": {},
    }

    logger.info(
        "🎯 Targeted measurement read: entity=%s feature=%s measurementId=%s",
        entity_list,
        feature,
        measurement_id,
    )
    return await send_spine_read(
        ws,
        address_source=src,
        address_destination=dst,
        cmd=cmd,
        msg_counter=msg_counter,
        ack_request=True,
    )

async def subscribe_remote_measurement(ws, *, local_device_address: str, remote_device_address: str, remote_measurement_feature: Dict[str, Any], msg_counter: MsgCounter):
    entity_list = remote_measurement_feature.get("entity")
    feature = remote_measurement_feature.get("feature")
    if not isinstance(entity_list, list) or not entity_list or not all(isinstance(x, int) for x in entity_list): return
    if not isinstance(feature, int): return
    logger.info("📡 Sending measurement subscription request for server entity=%s feature=%s", entity_list, feature)
    src_nm = _spine_addr(device=local_device_address, entity=0, feature=0)
    dst_nm = _spine_addr(device=remote_device_address, entity=0, feature=0)
    local_meas_client = _spine_addr(device=local_device_address, entity=1, feature=1)
    remote_meas_server = {"device": remote_device_address, "entity": [int(x) for x in entity_list], "feature": int(feature)}
    sub_call = {"subscriptionRequest": {"clientAddress": local_meas_client, "serverAddress": remote_meas_server, "serverFeatureType": "Measurement"}}
    await send_spine_call(ws, address_source=src_nm, address_destination=dst_nm, cmd={"nodeManagementSubscriptionRequestCall": sub_call}, msg_counter=msg_counter, ack_request=True)

async def reply_node_management_detailed_discovery(ws, *, request_header: Dict[str, Any], local_device_address: str, msg_counter: MsgCounter):
    ref = request_header.get("msgCounter")
    if ref is None: return
    addresses = _make_spine_reply_addresses(request_header, local_device_address=local_device_address)
    if addresses is None: return
    address_source, address_destination = addresses
    reply_datagram: Dict[str, Any] = {
        "datagram": {
            "header": {"specificationVersion": request_header.get("specificationVersion", "1.3.0"), "addressSource": address_source, "addressDestination": address_destination, "msgCounter": await msg_counter.next(), "msgCounterReference": ref, "cmdClassifier": "reply"},
            "payload": {"cmd": [{"nodeManagementDetailedDiscoveryData": build_local_detailed_discovery(local_device_address), "function": "nodeManagementDetailedDiscoveryData"}]},
        }
    }
    await send_ship_data(ws, reply_datagram)

async def reply_device_classification_manufacturer_data(ws, *, request_header: Dict[str, Any], local_device_address: str, msg_counter: MsgCounter):
    ref = request_header.get("msgCounter")
    if ref is None: return
    addresses = _make_spine_reply_addresses(request_header, local_device_address=local_device_address)
    if addresses is None: return
    address_source, address_destination = addresses
    reply_datagram: Dict[str, Any] = {
        "datagram": {
            "header": {"specificationVersion": request_header.get("specificationVersion", "1.3.0"), "addressSource": address_source, "addressDestination": address_destination, "msgCounter": await msg_counter.next(), "msgCounterReference": ref, "cmdClassifier": "reply"},
            "payload": {"cmd": [{"deviceClassificationManufacturerData": build_device_classification_manufacturer_data(local_device_address)}]},
        }
    }
    await send_ship_data(ws, reply_datagram)

async def reply_device_classification_user_data(ws, *, request_header: Dict[str, Any], local_device_address: str, msg_counter: MsgCounter):
    ref = request_header.get("msgCounter")
    if ref is None: return
    addresses = _make_spine_reply_addresses(request_header, local_device_address=local_device_address)
    if addresses is None: return
    address_source, address_destination = addresses
    reply_datagram: Dict[str, Any] = {
        "datagram": {
            "header": {"specificationVersion": request_header.get("specificationVersion", "1.3.0"), "addressSource": address_source, "addressDestination": address_destination, "msgCounter": await msg_counter.next(), "msgCounterReference": ref, "cmdClassifier": "reply"},
            "payload": {"cmd": [{"deviceClassificationUserData": build_device_classification_user_data()}]},
        }
    }
    await send_ship_data(ws, reply_datagram)

async def handle_spine_read(ws, *, request_header: Dict[str, Any], cmd: Dict[str, Any], local_device_address: str, msg_counter: MsgCounter):
    if "nodeManagementDetailedDiscoveryData" in cmd:
        await reply_node_management_detailed_discovery(ws, request_header=request_header, local_device_address=local_device_address, msg_counter=msg_counter)
        return
    if "deviceClassificationManufacturerData" in cmd:
        await reply_device_classification_manufacturer_data(ws, request_header=request_header, local_device_address=local_device_address, msg_counter=msg_counter)
        return
    if "deviceClassificationUserData" in cmd:
        await reply_device_classification_user_data(ws, request_header=request_header, local_device_address=local_device_address, msg_counter=msg_counter)
        return

def _extract_measurement_description_entries(cmd: Dict[str, Any]) -> list[Dict[str, Any]]:
    """Return every measurementDescription entry offered by the peer.

    Vaillant/EEBUS payloads have appeared in more than one wrapper shape in the
    field.  This collector deliberately keeps the complete description object
    instead of reducing it to scope/unit/type.
    """
    entries: list[Dict[str, Any]] = []
    root = cmd.get("measurementDescriptionListData")

    def collect(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                collect(item)
            return
        if not isinstance(value, dict):
            return

        if "measurementId" in value:
            entries.append(dict(value))
            return

        found_wrapper = False
        for key in ("measurementDescriptionListData", "measurementDescriptionData"):
            if key in value:
                found_wrapper = True
                collect(value.get(key))

        # Do not recursively walk arbitrary dictionaries.  A description entry
        # can itself contain nested structures which must remain part of the
        # original entry and must not be mistaken for another description.
        if not found_wrapper:
            return

    collect(root)
    return entries


def parse_measurement_description(cmd: Dict[str, Any]) -> Dict[int, Dict[str, Any]]:
    """Build the lookup map while preserving each full description object."""
    desc_map: Dict[int, Dict[str, Any]] = {}
    for entry in _extract_measurement_description_entries(cmd):
        mid = entry.get("measurementId")
        if not isinstance(mid, int):
            continue
        # Preserve every field supplied by the VR921.  Consumers that only need
        # scopeType/unit/measurementType can keep reading those keys as before.
        desc_map[int(mid)] = dict(entry)
    return desc_map


def _measurement_description_catalog_file() -> str:
    return _env_str(
        "SHIP_MEASUREMENT_CATALOG_FILE",
        os.path.join(CERT_DIR, "vr921_measurement_descriptions.json"),
    )


def _measurement_unit_for_log(unit: Any) -> str:
    if isinstance(unit, dict):
        value = unit.get("unit") or unit.get("name")
        if value is not None:
            return str(value)
    if unit is None:
        return ""
    if isinstance(unit, (str, int, float, bool)):
        return str(unit)
    try:
        return json.dumps(unit, separators=(",", ":"), ensure_ascii=False, sort_keys=True)
    except Exception:
        return str(unit)


def _measurement_server_key(entity: list[int], feature: int) -> str:
    entity_part = "_".join(str(int(x)) for x in entity)
    return "e%s_f%d" % (entity_part, int(feature))


def _measurement_catalog_count(catalog: Dict[str, Any]) -> int:
    total = 0
    servers = catalog.get("servers")
    if not isinstance(servers, dict):
        return 0
    for server in servers.values():
        if not isinstance(server, dict):
            continue
        measurements = server.get("measurements")
        if isinstance(measurements, dict):
            total += len(measurements)
        unidentified = server.get("unidentifiedDescriptions")
        if isinstance(unidentified, list):
            total += len(unidentified)
    return total


def _load_measurement_description_catalog(remote_ski: str) -> Dict[str, Any]:
    path = _measurement_description_catalog_file()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        saved_remote_ski = _normalize_ski(data.get("remoteSki"))
        expected_remote_ski = _normalize_ski(remote_ski)
        if saved_remote_ski and expected_remote_ski and saved_remote_ski != expected_remote_ski:
            logger.warning(
                "⚠️ Existing measurement catalog belongs to another remote SKI (%s); starting a fresh catalog for %s",
                saved_remote_ski,
                expected_remote_ski,
            )
            return {}
        servers = data.get("servers")
        if not isinstance(servers, dict):
            data["servers"] = {}
        return data
    except Exception as exc:
        logger.warning("⚠️ Could not read existing measurement description catalog: %s", exc)
        return {}


def _persist_measurement_description_catalog(catalog: Dict[str, Any]) -> None:
    path = _measurement_description_catalog_file()
    tmp = path + ".tmp"
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(catalog, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def capture_measurement_descriptions(
    cmd: Dict[str, Any],
    *,
    source_address: Optional[Dict[str, Any]],
    catalog: Dict[str, Any],
    entity_labels: Dict[Tuple[int, ...], str],
    remote_device_address: Optional[str],
    remote_ski: str,
    local_ship_id: str,
    local_ski: str,
) -> Dict[int, Dict[str, Any]]:
    """Capture, log and persist all descriptions from one Measurement server."""
    entries = _extract_measurement_description_entries(cmd)
    desc_map = parse_measurement_description(cmd)

    entity: list[int] = []
    feature: Optional[int] = None
    if isinstance(source_address, dict):
        raw_entity = source_address.get("entity")
        raw_feature = source_address.get("feature")
        if isinstance(raw_entity, list) and all(isinstance(x, int) for x in raw_entity):
            entity = [int(x) for x in raw_entity]
        if isinstance(raw_feature, int):
            feature = int(raw_feature)

    if not entity or feature is None:
        logger.warning(
            "⚠️ measurementDescriptionListData without usable source address: %s",
            source_address,
        )
        return desc_map

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    catalog["schemaVersion"] = 1
    catalog["updatedAt"] = now
    catalog["localShipId"] = local_ship_id
    catalog["localSki"] = _normalize_ski(local_ski)
    catalog["remoteSki"] = _normalize_ski(remote_ski)
    if remote_device_address:
        catalog["remoteDeviceAddress"] = remote_device_address

    servers = catalog.setdefault("servers", {})
    if not isinstance(servers, dict):
        servers = {}
        catalog["servers"] = servers

    server_key = _measurement_server_key(entity, feature)
    server = servers.get(server_key)
    if not isinstance(server, dict):
        server = {}
        servers[server_key] = server

    entity_label = entity_labels.get(tuple(entity), "")
    server["entity"] = entity
    server["feature"] = feature
    server["entityDescription"] = entity_label
    server["lastSeenAt"] = now

    measurements = server.get("measurements")
    if not isinstance(measurements, dict):
        measurements = {}
        server["measurements"] = measurements

    runtime_availability = server.get("runtimeAvailability")
    if not isinstance(runtime_availability, dict):
        runtime_availability = {}
        server["runtimeAvailability"] = runtime_availability

    unidentified: list[Dict[str, Any]] = []
    active_measurement_ids: list[int] = []
    changed = 0
    log_all = _env_bool("SHIP_LOG_MEASUREMENT_DESCRIPTIONS", True)

    logger.info(
        "📚 Capturing %d measurement description(s) from entity=%s feature=%s%s",
        len(entries),
        entity,
        feature,
        " (%s)" % entity_label if entity_label else "",
    )

    for entry in entries:
        # The data arrived through JSON, therefore it is already JSON-safe.  A
        # serialize/deserialize clone also prevents later in-memory mutation.
        try:
            full_entry = json.loads(json.dumps(entry, ensure_ascii=False))
        except Exception:
            full_entry = dict(entry)

        mid = full_entry.get("measurementId")
        if isinstance(mid, int):
            mid_int = int(mid)
            active_measurement_ids.append(mid_int)
            key = str(mid_int)
            previous = measurements.get(key)
            if previous != full_entry:
                changed += 1
            measurements[key] = full_entry
            runtime_state = runtime_availability.get(key)
            if not isinstance(runtime_state, dict):
                runtime_availability[key] = {
                    "status": "advertised",
                    "lastCheckedAt": now,
                    "observedVia": "description",
                }
        else:
            unidentified.append(full_entry)

        if log_all:
            scope = full_entry.get("scopeType")
            unit = _measurement_unit_for_log(full_entry.get("unit"))
            mtype = full_entry.get("measurementType")
            logger.info(
                "📋 Measurement description: entity=%s feature=%s id=%s scope=%s unit=%s type=%s raw=%s",
                entity,
                feature,
                mid,
                scope,
                unit,
                mtype,
                json.dumps(full_entry, separators=(",", ":"), ensure_ascii=False, sort_keys=True),
            )

    # Keep malformed/unidentified descriptions too; 'all' means they must not
    # silently disappear just because a measurementId is absent or unexpected.
    server["unidentifiedDescriptions"] = unidentified
    server["activeMeasurementIds"] = sorted(set(active_measurement_ids))
    server["receivedDescriptionCount"] = len(entries)
    server["identifiedDescriptionCount"] = len(measurements)

    try:
        _persist_measurement_description_catalog(catalog)
        logger.info(
            "💾 Measurement description catalog updated: %s (%d total description(s), %d changed/new)",
            _measurement_description_catalog_file(),
            _measurement_catalog_count(catalog),
            changed,
        )
    except Exception as exc:
        logger.warning("⚠️ Could not persist measurement description catalog: %s", exc)

    return desc_map


def _extract_measurement_data_entries(cmd: Dict[str, Any]) -> list[Dict[str, Any]]:
    """Return raw MeasurementData entries from all observed wrapper shapes."""
    entries: list[Dict[str, Any]] = []
    root = cmd.get("measurementListData")

    def collect(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                collect(item)
            return
        if not isinstance(value, dict):
            return
        if "measurementId" in value:
            entries.append(dict(value))
            return
        for key in ("measurementListData", "measurementData"):
            if key in value:
                collect(value.get(key))

    collect(root)
    return entries


def _refresh_runtime_availability_lists(server: Dict[str, Any]) -> None:
    runtime = server.get("runtimeAvailability")
    if not isinstance(runtime, dict):
        return
    available: list[int] = []
    no_current_value: list[int] = []
    for key, state in runtime.items():
        if not isinstance(state, dict):
            continue
        try:
            mid = int(key)
        except (TypeError, ValueError):
            continue
        status = state.get("status")
        if status == "valueAvailable":
            available.append(mid)
        elif status == "noCurrentValue":
            no_current_value.append(mid)
    server["runtimeValueAvailableMeasurementIds"] = sorted(available)
    server["runtimeNoCurrentValueMeasurementIds"] = sorted(no_current_value)


def _record_measurement_runtime_state(
    catalog: Dict[str, Any],
    *,
    source_address: Optional[Dict[str, Any]],
    measurement_id: int,
    status: str,
    observed_via: str,
    request_msg_counter: Optional[int] = None,
    reply_header: Optional[Dict[str, Any]] = None,
    value: Optional[float] = None,
    unit: str = "",
) -> None:
    if not isinstance(source_address, dict):
        return
    entity = source_address.get("entity")
    feature = source_address.get("feature")
    if not (isinstance(entity, list) and entity and all(isinstance(x, int) for x in entity)):
        return
    if not isinstance(feature, int):
        return

    servers = catalog.get("servers")
    if not isinstance(servers, dict):
        return
    server_key = _measurement_server_key([int(x) for x in entity], int(feature))
    server = servers.get(server_key)
    if not isinstance(server, dict):
        return

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    runtime = server.get("runtimeAvailability")
    if not isinstance(runtime, dict):
        runtime = {}
        server["runtimeAvailability"] = runtime

    key = str(int(measurement_id))
    state = runtime.get(key)
    if not isinstance(state, dict):
        state = {}
        runtime[key] = state

    state["status"] = status
    state["lastCheckedAt"] = now
    state["observedVia"] = observed_via
    if request_msg_counter is not None:
        state["lastRequestMsgCounter"] = int(request_msg_counter)
    if isinstance(reply_header, dict):
        reply_counter = reply_header.get("msgCounter")
        reply_ref = reply_header.get("msgCounterReference")
        if isinstance(reply_counter, int):
            state["lastReplyMsgCounter"] = int(reply_counter)
        if isinstance(reply_ref, int):
            state["lastReplyMsgCounterReference"] = int(reply_ref)
    if value is not None:
        state["lastValue"] = float(value)
        state["lastValueAt"] = now
        if unit:
            state["unit"] = unit

    _refresh_runtime_availability_lists(server)
    catalog["updatedAt"] = now
    try:
        _persist_measurement_description_catalog(catalog)
    except Exception as exc:
        logger.warning("⚠️ Could not persist measurement runtime availability: %s", exc)


def _record_observed_measurement_updates(
    catalog: Dict[str, Any],
    *,
    updates: list[Dict[str, Any]],
    source_address: Optional[Dict[str, Any]],
    observed_via: str,
    reply_header: Optional[Dict[str, Any]] = None,
) -> None:
    for update in updates:
        mid = update.get("measurementId")
        value = update.get("value")
        if not isinstance(mid, int) or not isinstance(value, (int, float)):
            continue
        _record_measurement_runtime_state(
            catalog,
            source_address=source_address,
            measurement_id=mid,
            status="valueAvailable",
            observed_via=observed_via,
            reply_header=reply_header,
            value=float(value),
            unit=_unit_to_ha(update.get("unit")),
        )


def _runtime_measurement_ids_with_status(
    catalog: Dict[str, Any],
    *,
    entity: list[int],
    feature: int,
    status: str,
) -> list[int]:
    servers = catalog.get("servers")
    if not isinstance(servers, dict):
        return []
    server = servers.get(_measurement_server_key(entity, feature))
    if not isinstance(server, dict):
        return []
    runtime = server.get("runtimeAvailability")
    if not isinstance(runtime, dict):
        return []

    result: list[int] = []
    for key, state in runtime.items():
        if not isinstance(state, dict) or state.get("status") != status:
            continue
        try:
            mid = int(key)
        except (TypeError, ValueError):
            continue
        if mid >= 0:
            result.append(mid)
    return sorted(set(result))


def _find_ac_power_total_update(updates: list[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for update in updates:
        if not isinstance(update, dict):
            continue
        scope = str(update.get("scopeType") or "").lower()
        mid = update.get("measurementId")
        source = update.get("source") if isinstance(update.get("source"), dict) else {}
        entity = source.get("entity") if isinstance(source, dict) else None
        if entity == [3, 1] and (scope == "acpowertotal" or mid == 9):
            if isinstance(update.get("value"), (int, float)):
                return update
    return None


def parse_measurement_list(cmd: Dict[str, Any], desc_map: Dict[int, Dict[str, Any]], *, source_address: Optional[Dict[str, Any]] = None, entity_labels: Optional[Dict[Tuple[int, ...], str]] = None) -> list[Dict[str, Any]]:
    def _scaled_number_to_float(v: Any) -> Optional[float]:
        if not isinstance(v, dict): return None
        number = v.get("number")
        scale = v.get("scale", 0)
        try: num = float(number)
        except (TypeError, ValueError): return None
        try: scale_int = int(scale)
        except (TypeError, ValueError): scale_int = 0
        try: return num * (10.0 ** float(scale_int))
        except Exception: return None

    ml = cmd.get("measurementListData")
    ml_list: Optional[list] = None
    if isinstance(ml, list): ml_list = ml
    elif isinstance(ml, dict):
        if isinstance(ml.get("measurementListData"), list): ml_list = cast(list, ml.get("measurementListData"))
        elif isinstance(ml.get("measurementData"), list): ml_list = cast(list, ml.get("measurementData"))

    if not isinstance(ml_list, list): return []

    updates: list[Dict[str, Any]] = []
    src_entity: Optional[list[int]] = None
    src_feature: Optional[int] = None
    if isinstance(source_address, dict):
        ent = source_address.get("entity")
        feat = source_address.get("feature")
        if isinstance(ent, list) and all(isinstance(x, int) for x in ent): src_entity = [int(x) for x in ent]
        if isinstance(feat, int): src_feature = int(feat)

    for entry in ml_list:
        if not isinstance(entry, dict): continue
        mid = entry.get("measurementId")
        mdata = entry.get("measurementData")
        if not isinstance(mid, int): continue
        
        val = None
        if isinstance(mdata, dict): val = _scaled_number_to_float(mdata.get("value"))
        if val is None: val = _scaled_number_to_float(entry.get("value"))
        if val is None: continue

        meta = desc_map.get(mid, {})
        scope = meta.get("scopeType") or "unknown"
        unit = meta.get("unit") or ""
        mtype = meta.get("measurementType") or ""

        if isinstance(unit, dict): unit = unit.get("unit") or unit.get("name") or json.dumps(unit, separators=(",", ":"), ensure_ascii=False)
        scope_str = scope if isinstance(scope, str) else str(scope)
        unit_str = unit if isinstance(unit, str) else str(unit)
        mtype_str = mtype if isinstance(mtype, str) else str(mtype)

        # Accept every measurement for which the VR921 delivered a numeric value.
        # Do not infer L1/L2/L3 for repeated AC measurements: the description
        # currently exposes scope/type/unit but no proven phase designation.
        source_label = ""
        if src_entity and entity_labels:
            source_label = str(entity_labels.get(tuple(src_entity)) or "")

        update = {
            "measurementId": mid,
            "scopeType": scope_str,
            "unit": unit_str,
            "measurementType": mtype_str,
            "commodityType": meta.get("commodityType") or "",
            "value": val,
            "source": {"entity": src_entity, "feature": src_feature, "description": source_label},
        }
        updates.append(update)
        logger.info("🔧 Parsed update accepted: %s", update)

        friendly_name = _friendly_sensor_name(
            scope_str,
            source_entity=src_entity,
            source_label=source_label,
            measurement_id=mid,
        )
        entity_desc = " (entity=%s)" % src_entity if src_entity else ""
        icon = _measurement_log_icon(mtype_str, scope_str)

        logger.info(
            "%s %s: %s %s (ID %s, scope=%s, type=%s)%s",
            icon,
            friendly_name,
            val,
            unit_str,
            mid,
            scope_str,
            mtype_str or "unknown",
            entity_desc,
        )

    return updates

# ---------------------------------------------------------------------------
# SHIP handshake
# ---------------------------------------------------------------------------
async def send_access_methods(ws, local_ship_id: str):
    """Sendet unsere lokale SHIP-ID als Access Method."""
    await send_ship_json(
        ws,
        {
            "accessMethods": {
                "id": local_ship_id
            }
        }
    )

async def perform_ship_handshake(ws, local_ship_id: str):
    init_ack = await ws.recv()
    logger.info("📤 [HELLO] Sending connectionHello (phase: ready)...")
    await send_ship_json(ws, {"connectionHello": {"phase": "ready", "waiting": 60000}})

    state = "WAITING_HELLO"
    last_pending_hello_sent = 0.0
    
    while True:
        try:
            raw_msg = await ws.recv()
        except Exception as exc:
            if _connection_close_code(exc) is not None:
                raise
            logger.warning("⚠️ SHIP handshake receive failed: %s", exc)
            return False
        if not isinstance(raw_msg, bytes) or len(raw_msg) < 2: continue
        if raw_msg[0] != 0x01: continue

        payload_text = json_from_eebus_json(raw_msg[1:].decode("utf-8", errors="ignore"))
        try: msg = json.loads(payload_text)
        except json.JSONDecodeError: continue

        if "connectionHello" in msg and state == "WAITING_HELLO":
            hello = msg.get("connectionHello") or {}
            phase = hello.get("phase")
            if phase == "pending":
                logger.info("⏳ [HELLO] STATUS: PENDING - waiting for confirmation in the myVAILLANT app...")
                now = time.monotonic()
                if now - last_pending_hello_sent > 5.0:
                    await send_ship_json(ws, {"connectionHello": {"phase": "pending", "waiting": 60000}})
                    last_pending_hello_sent = now
            elif phase == "ready":
                logger.info("✅ [HELLO] Phase complete - both sides READY")
                logger.info("📤 [PROTOCOL] Sending messageProtocolHandshake...")
                await send_ship_json(ws, {"messageProtocolHandshake": {"handshakeType": "announceMax", "version": {"major": 1, "minor": 0}, "formats": {"format": ["JSON-UTF8"]}}})
                state = "WAITING_PROTOCOL"
            elif phase == "aborted":
                return False

        elif "messageProtocolHandshake" in msg and state == "WAITING_PROTOCOL":
            handshake = msg.get("messageProtocolHandshake") or {}
            if handshake.get("handshakeType") != "select": return False
            logger.info("✅ [PROTOCOL] Protocol handshake confirmed")
            logger.info("📤 [PROTOCOL] Confirm selection (select)...")
            await send_ship_json(ws, {"messageProtocolHandshake": {"handshakeType": "select", "version": {"major": 1, "minor": 0}, "formats": {"format": ["JSON-UTF8"]}}})
            logger.info("📤 [PIN] Sending connectionPinState (none)...")
            await send_ship_json(ws, {"connectionPinState": {"pinState": "none"}})
            state = "WAITING_PIN"

        elif "connectionPinState" in msg and state == "WAITING_PIN":
            logger.info("📤 [ACCESS] Sending accessMethodsRequest...")
            await send_ship_json(ws, {"accessMethodsRequest": {}})
            state = "WAITING_ACCESS"

        elif "accessMethodsRequest" in msg and state == "WAITING_ACCESS":
            logger.info("📥 [ACCESS] accessMethodsRequest received from device -> sending accessMethods...")
            await send_access_methods(ws, local_ship_id)

        elif "accessMethods" in msg and state == "WAITING_ACCESS":
            logger.info("💎 SHIP handshake completed successfully!")
            return True

async def main():
    logger.info("🚀 EEBUS SHIP Client started")
    logger.info("📊 Measurement parser: all VR921 numeric measurements enabled (no inferred phase mapping)")
    logger.info("🎯 Measurement acquisition: targeted per-ID SPINE reads enabled")
    logger.info("🧪 Measurement availability tracking: advertised vs current-value runtime state enabled")
    compressor_active_on_w = max(1.0, _env_float("SHIP_COMPRESSOR_ACTIVE_ON_W", 100.0))
    compressor_active_off_w = max(0.0, _env_float("SHIP_COMPRESSOR_ACTIVE_OFF_W", 50.0))
    if compressor_active_off_w >= compressor_active_on_w:
        compressor_active_off_w = compressor_active_on_w * 0.5
    compressor_reprobe_seconds = max(10.0, _env_float("SHIP_COMPRESSOR_REPROBE_SECONDS", 60.0))
    logger.info(
        "🔁 Compressor re-probe enabled: active_on=%.1f W active_off=%.1f W interval=%.1f s",
        compressor_active_on_w,
        compressor_active_off_w,
        compressor_reprobe_seconds,
    )
    my_ski = get_or_create_certificate()
    local_ship_id = f"python-{my_ski[:12]}"
    desc = {
        "txtvers": "1",
        "id": local_ship_id,
        "path": "/ship/",
        "ski": my_ski,
        "register": "true",
        "brand": "Python",
        "type": "EnergyManagementSystem",
        "model": "EEBUS-Python-Client",
        "serial": local_ship_id,
        "cat": "2",
    }
    local_device_address = f"d:_i:1_{local_ship_id}"
    msg_counter = MsgCounter(start=1)

    ha_device_id = _slug(_env_str("HA_DEVICE_ID", f"eebus_{local_ship_id}"))
    ha_device_name = _env_str("HA_DEVICE_NAME", "EEBUS HeatPump")
    mqtt_pub = HAMqttPublisher(device_id=ha_device_id, device_name=ha_device_name)
    
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(("8.8.8.8", 80))
    local_ip = s.getsockname()[0]
    s.close()

    aiozc = AsyncZeroconf(ip_version=IPVersion.V4Only)
    handler = MDNSHandler(my_ski, local_ip)
    peer_pin = _load_peer_pin()
    trust_retry_seconds = max(5, _env_int("SHIP_TRUST_RETRY_SECONDS", 10))
    reconnect_seconds = max(5, _env_int("SHIP_RECONNECT_SECONDS", 10))

    logger.info("🔐 Local EEBUS identity: SHIP-ID=%s SKI=%s", local_ship_id, my_ski)
    if peer_pin.get("ski"):
        logger.info("🔒 Pinned VR921 SKI: %s", peer_pin.get("ski"))

    service_name_base = f"Python-{my_ski[:6]}"
    service_name = f"{service_name_base}._ship._tcp.local."
    info = AsyncServiceInfo("_ship._tcp.local.", service_name,
                            addresses=[socket.inet_aton(local_ip)], port=54885, properties=desc)
    try:
        await aiozc.async_register_service(info)
    except Exception as exc:
        logger.warning(f"⚠️ Zeroconf name collision or registration failure: {exc}. Retrying with a unique service name.")
        service_name = f"{service_name_base}-{uuid.uuid4().hex[:6]}._ship._tcp.local."
        info = AsyncServiceInfo("_ship._tcp.local.", service_name,
                                addresses=[socket.inet_aton(local_ip)], port=54885, properties=desc)
        await aiozc.async_register_service(info)
    AsyncServiceBrowser(aiozc.zeroconf, "_ship._tcp.local.", handler)
    
    try:
        while True:  # Retry loop for connections
            try:
                logger.info("🔎 Searching for VR921...")
                timeout = 30
                elapsed = 0
                while handler.target_info is None and elapsed < timeout:
                    await asyncio.sleep(1)
                    elapsed += 1
            
                if handler.target_info is None:
                    logger.error("❌ No VR921 found. Retrying in 60 seconds...")
                    await asyncio.sleep(60)
                    continue
                
                target_ip = socket.inet_ntoa(handler.target_info.addresses[0])
                target_port = handler.target_info.port
                target_ski = _advertised_ski(handler.target_info)
                logger.info(f"✅ VR921 found: {target_ip}:{target_port}")
                logger.info("🔎 VR921 advertised SKI: %s", target_ski or "<invalid>")

                ssl_ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
                ssl_ctx.minimum_version = ssl.TLSVersion.TLSv1_2
                ssl_ctx.load_cert_chain(certfile=os.path.join(CERT_DIR, "cert.pem"), keyfile=os.path.join(CERT_DIR, "key.pem"))
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = ssl.CERT_NONE
                ssl_ctx.set_ciphers('HIGH:!aNULL:!eNULL:!MD5@SECLEVEL=1')

                import websockets
                async with websockets.connect(
                    f"wss://{target_ip}:{target_port}/ship/",
                    ssl=ssl_ctx,
                    subprotocols=cast(Any, ["ship"]),
                    open_timeout=20,
                    ping_interval=None,
                    ping_timeout=None,
                    close_timeout=10,
                ) as ws:
                    peer_ski, peer_fingerprint = _verify_peer_certificate(
                        ws, advertised_ski=target_ski, pinned=peer_pin
                    )
                    logger.info("🔒 VR921 TLS identity verified: SKI=%s", peer_ski)

                    await ws.send(b'\x00\x00')

                    try:
                        success = await asyncio.wait_for(perform_ship_handshake(ws, local_ship_id), timeout=120)
                    except asyncio.TimeoutError:
                        logger.error("❌ Handshake timed out")
                        await asyncio.sleep(reconnect_seconds)
                        continue
                    except Exception as exc:
                        close_code = _connection_close_code(exc)
                        if close_code == 4452:
                            logger.warning("🔐 WAITING_FOR_TRUST: VR921 rejected this EEBUS node (4452)")
                            logger.warning("   Local SKI: %s", my_ski)
                            logger.warning("   SHIP-ID:   %s", local_ship_id)
                            logger.warning("   Approve this device in myVAILLANT → Settings → Network settings → EEBUS")
                            logger.info("🔄 Retrying trust check in %d seconds...", trust_retry_seconds)
                            await asyncio.sleep(trust_retry_seconds)
                            continue
                        raise

                    if not success:
                        logger.error("❌ Handshake failed")
                        await asyncio.sleep(reconnect_seconds)
                        continue

                    if peer_pin.get("ski") != peer_ski or peer_pin.get("certificate_sha256") != peer_fingerprint:
                        _store_peer_pin(
                            ski=peer_ski,
                            fingerprint=peer_fingerprint,
                            name=str(handler.target_name or "VR921"),
                        )
                        peer_pin = {
                            "ski": peer_ski,
                            "certificate_sha256": peer_fingerprint,
                            "name": str(handler.target_name or "VR921"),
                        }
                        logger.info("🔐 Saved verified VR921 peer identity")

                    logger.info("🎯 SHIP layer is up! Waiting for SPINE data...")

                    await asyncio.to_thread(mqtt_pub.connect)
                    
                    message_count = 0
                    remote_device_address: Optional[str] = None
                    discovery_requested = False
                    peer_use_case_received = False
                    measurement_subscription_sent = False
                    polled_once = False
                    
                    measurement_desc_maps: Dict[tuple[tuple[int, ...], int], Dict[int, Dict[str, Any]]] = {}
                    targeted_measurement_ids_requested: Dict[tuple[tuple[int, ...], int], set[int]] = {}
                    pending_targeted_measurement_reads: Dict[int, Dict[str, Any]] = {}
                    measurement_description_catalog = _load_measurement_description_catalog(peer_ski)
                    measurement_description_catalog["schemaVersion"] = 1
                    measurement_description_catalog["localShipId"] = local_ship_id
                    measurement_description_catalog["localSki"] = my_ski
                    measurement_description_catalog["remoteSki"] = peer_ski
                    if not isinstance(measurement_description_catalog.get("servers"), dict):
                        measurement_description_catalog["servers"] = {}
                    ha_published: set[str] = set()
                    selected_measurement_servers: list[Dict[str, Any]] = []
                    remote_entity_labels: Dict[Tuple[int, ...], str] = {}
                    compressor_active = False
                    compressor_last_reprobe_at = 0.0

                    async def _maybe_reprobe_compressor_measurements(
                        updates: list[Dict[str, Any]],
                        *,
                        observed_via: str,
                    ) -> None:
                        nonlocal compressor_active, compressor_last_reprobe_at

                        total_update = _find_ac_power_total_update(updates)
                        if not isinstance(total_update, dict):
                            return
                        raw_power = total_update.get("value")
                        if not isinstance(raw_power, (int, float)):
                            return
                        power_w = float(raw_power)
                        now_mono = time.monotonic()

                        became_active = False
                        if not compressor_active and power_w >= compressor_active_on_w:
                            compressor_active = True
                            became_active = True
                            logger.info(
                                "🔥 Compressor became active: acPowerTotal=%.1f W (%s)",
                                power_w,
                                observed_via,
                            )
                        elif compressor_active and power_w <= compressor_active_off_w:
                            compressor_active = False
                            logger.info(
                                "🧊 Compressor returned to standby: acPowerTotal=%.1f W (%s)",
                                power_w,
                                observed_via,
                            )
                            return

                        if not compressor_active:
                            return
                        if not became_active and (now_mono - compressor_last_reprobe_at) < compressor_reprobe_seconds:
                            return
                        if remote_device_address is None:
                            return

                        candidate_ids = _runtime_measurement_ids_with_status(
                            measurement_description_catalog,
                            entity=[3, 1],
                            feature=11,
                            status="noCurrentValue",
                        )
                        candidate_ids = [mid for mid in candidate_ids if mid != 9]
                        pending_ids = {
                            int(item.get("measurementId"))
                            for item in pending_targeted_measurement_reads.values()
                            if isinstance(item, dict) and isinstance(item.get("measurementId"), int)
                            and item.get("sourceKey") == ((3, 1), 11)
                        }
                        candidate_ids = [mid for mid in candidate_ids if mid not in pending_ids]
                        if not candidate_ids:
                            compressor_last_reprobe_at = now_mono
                            return

                        logger.info(
                            "🔁 Compressor active at %.1f W: re-probing %d noCurrentValue measurement ID(s): %s",
                            power_w,
                            len(candidate_ids),
                            candidate_ids,
                        )
                        remote_feature = {"entity": [3, 1], "feature": 11}
                        for measurement_id in candidate_ids:
                            request_counter = await request_remote_measurement_id(
                                ws,
                                local_device_address=local_device_address,
                                remote_device_address=remote_device_address,
                                remote_measurement_feature=remote_feature,
                                measurement_id=measurement_id,
                                msg_counter=msg_counter,
                            )
                            if isinstance(request_counter, int):
                                pending_targeted_measurement_reads[request_counter] = {
                                    "measurementId": int(measurement_id),
                                    "sourceKey": ((3, 1), 11),
                                    "reason": "compressorActiveReprobe",
                                    "powerW": power_w,
                                }
                        compressor_last_reprobe_at = now_mono

                    def _desc_key_from_address(addr: Any) -> Optional[tuple[tuple[int, ...], int]]:
                        if not isinstance(addr, dict): return None
                        ent = addr.get("entity")
                        feat = addr.get("feature")
                        if not (isinstance(ent, list) and ent and all(isinstance(x, int) for x in ent)): return None
                        if not isinstance(feat, int): return None
                        return (tuple(int(x) for x in ent), int(feat))

                    while True:
                        data = None
                        try:
                            data = await asyncio.wait_for(ws.recv(), timeout=300)
                        except asyncio.TimeoutError:
                            logger.debug("⏳ SPINE receive timeout, connection still alive")
                            continue
                        else:
                            message_count += 1
                            logger.info(f"📨 SPINE frame received (#%d) length=%d", message_count, len(data) if isinstance(data, (bytes, str)) else 0)

                        if isinstance(data, bytes) and len(data) > 0:
                            if data[0] == 1:
                                logger.debug("🔁 Received SHIP keepalive / acknowledgement frame")
                                pass
                            elif data[0] == 2:
                                try:
                                    payload_text = data[1:].decode("utf-8", errors="ignore")
                                    payload_text = json_from_eebus_json(payload_text)
                                    msg = json.loads(payload_text)

                                    parsed = _parse_spine_datagram(msg)
                                    if parsed is None: continue

                                    hdr, cmd = parsed
                                    cmd_classifier = hdr.get("cmdClassifier")

                                    if remote_device_address is None:
                                        addr_src = hdr.get("addressSource")
                                        if isinstance(addr_src, dict):
                                            dev = addr_src.get("device")
                                            if isinstance(dev, str) and dev:
                                                remote_device_address = dev

                                    if remote_device_address is not None and not discovery_requested:
                                        discovery_requested = True
                                        await request_remote_detailed_discovery(ws, local_device_address=local_device_address, remote_device_address=remote_device_address, msg_counter=msg_counter)

                                    try:
                                        if cmd_classifier != "result":
                                            await send_spine_result_ok(ws, request_header=hdr, local_device_address=local_device_address, msg_counter=msg_counter)

                                        if cmd_classifier == "read":
                                            await handle_spine_read(ws, request_header=hdr, cmd=cmd, local_device_address=local_device_address, msg_counter=msg_counter)
                                                
                                        elif cmd_classifier == "reply":
                                            if "nodeManagementDetailedDiscoveryData" in cmd:
                                                discovery = cmd.get("nodeManagementDetailedDiscoveryData")
                                                if isinstance(discovery, dict):
                                                    remote_entity_labels = _extract_entity_labels(discovery)
                                                    if remote_entity_labels:
                                                        logger.info("🧭 Remote entity labels from discovery: %s", remote_entity_labels)
                                                    remote_measurement_servers = _extract_measurement_servers(discovery)
                                                    if remote_measurement_servers:
                                                        selected_measurement_servers = list(remote_measurement_servers)
                                                        logger.info("✅ Selected %d remote measurement server(s): %s", len(selected_measurement_servers), selected_measurement_servers)
                                                    else:
                                                        logger.warning("⚠️ Discovery returned no usable measurement servers")

                                                    if remote_device_address is not None:
                                                        logger.info("🔎 Remote device identified: %s", remote_device_address)
                                                        await request_remote_node_management_use_case_data(ws, local_device_address=local_device_address, remote_device_address=remote_device_address, msg_counter=msg_counter)
                                                        logger.info("📤 Requested remote node management use case data")
                                                        
                                                    if peer_use_case_received and selected_measurement_servers and not measurement_subscription_sent:
                                                        logger.info("▶ Triggering measurement subscription because peer use case has already been received")
                                                        measurement_subscription_sent = True
                                                        logger.info("📡 Subscribing to %d remote measurement servers", len(selected_measurement_servers))
                                                        for server in selected_measurement_servers:
                                                            await subscribe_remote_measurement(ws, local_device_address=local_device_address, remote_device_address=remote_device_address, remote_measurement_feature=server, msg_counter=msg_counter)
                                                        logger.info("📤 Measurement subscription request(s) sent")
                                                        if not polled_once:
                                                            polled_once = True
                                                            logger.info("⏱️ Initial poll for all measurement values")
                                                            for server in selected_measurement_servers:
                                                                await request_remote_measurement_once(ws, local_device_address=local_device_address, remote_device_address=remote_device_address, remote_measurement_feature=server, msg_counter=msg_counter)
                                                                logger.info("📤 Measurement discovery poll sent for server: entity=%s feature=%s", server.get("entity"), server.get("feature"))

                                            elif "nodeManagementUseCaseData" in cmd:
                                                peer_use_case_received = True
                                                logger.info("✅ Remote peer use case data received")
                                                if selected_measurement_servers and not measurement_subscription_sent:
                                                    logger.info("▶ Triggering measurement subscription because measurement servers are already known")
                                                    measurement_subscription_sent = True
                                                    logger.info("📡 Subscribing to %d remote measurement servers", len(selected_measurement_servers))
                                                    for server in selected_measurement_servers:
                                                        await subscribe_remote_measurement(ws, local_device_address=local_device_address, remote_device_address=remote_device_address, remote_measurement_feature=server, msg_counter=msg_counter)
                                                    logger.info("📤 Measurement subscription request(s) sent")
                                                    if not polled_once:
                                                        polled_once = True
                                                        logger.info("⏱️ Initial poll for all measurement values")
                                                        for server in selected_measurement_servers:
                                                            await request_remote_measurement_once(ws, local_device_address=local_device_address, remote_device_address=remote_device_address, remote_measurement_feature=server, msg_counter=msg_counter)
                                                            logger.info("📤 Measurement discovery poll sent for server: entity=%s feature=%s", server.get("entity"), server.get("feature"))

                                            elif "measurementDescriptionListData" in cmd:
                                                source_address = hdr.get("addressSource") if isinstance(hdr, dict) else None
                                                logger.info("📥 Received measurementDescriptionListData reply from %s", source_address)
                                                desc_map = capture_measurement_descriptions(
                                                    cmd,
                                                    source_address=source_address if isinstance(source_address, dict) else None,
                                                    catalog=measurement_description_catalog,
                                                    entity_labels=remote_entity_labels,
                                                    remote_device_address=remote_device_address,
                                                    remote_ski=peer_ski,
                                                    local_ship_id=local_ship_id,
                                                    local_ski=my_ski,
                                                )
                                                key = _desc_key_from_address(source_address)
                                                if key is not None:
                                                    measurement_desc_maps[key] = desc_map

                                                    already_requested = targeted_measurement_ids_requested.setdefault(key, set())
                                                    missing_ids = [
                                                        mid for mid in sorted(desc_map.keys())
                                                        if isinstance(mid, int) and mid >= 0 and mid not in already_requested
                                                    ]
                                                    if missing_ids and remote_device_address is not None:
                                                        logger.info(
                                                            "🎯 Requesting %d advertised measurement ID(s) individually from entity=%s feature=%s: %s",
                                                            len(missing_ids),
                                                            list(key[0]),
                                                            key[1],
                                                            missing_ids,
                                                        )
                                                        remote_feature = {"entity": list(key[0]), "feature": int(key[1])}
                                                        for measurement_id in missing_ids:
                                                            request_counter = await request_remote_measurement_id(
                                                                ws,
                                                                local_device_address=local_device_address,
                                                                remote_device_address=remote_device_address,
                                                                remote_measurement_feature=remote_feature,
                                                                measurement_id=measurement_id,
                                                                msg_counter=msg_counter,
                                                            )
                                                            if isinstance(request_counter, int):
                                                                pending_targeted_measurement_reads[request_counter] = {
                                                                    "measurementId": int(measurement_id),
                                                                    "sourceKey": key,
                                                                }
                                                            already_requested.add(measurement_id)

                                            elif "measurementListData" in cmd:
                                                logger.info("📥 Received measurementListData reply from %s", hdr.get("addressSource"))
                                                key = _desc_key_from_address(hdr.get("addressSource"))
                                                desc_map = measurement_desc_maps.get(key, {}) if key is not None else {}
                                                source_address = hdr.get("addressSource") if isinstance(hdr, dict) else None
                                                updates = parse_measurement_list(cmd, desc_map, source_address=source_address if isinstance(source_address, dict) else None, entity_labels=remote_entity_labels)
                                                logger.info("📊 Parsed %d numeric measurement update(s) from reply", len(updates))

                                                _record_observed_measurement_updates(
                                                    measurement_description_catalog,
                                                    updates=updates,
                                                    source_address=source_address if isinstance(source_address, dict) else None,
                                                    observed_via="reply",
                                                    reply_header=hdr if isinstance(hdr, dict) else None,
                                                )
                                                await _maybe_reprobe_compressor_measurements(updates, observed_via="reply")

                                                reply_ref = hdr.get("msgCounterReference") if isinstance(hdr, dict) else None
                                                pending_target = pending_targeted_measurement_reads.pop(reply_ref, None) if isinstance(reply_ref, int) else None
                                                if isinstance(pending_target, dict):
                                                    expected_id = pending_target.get("measurementId")
                                                    if isinstance(expected_id, int):
                                                        expected_update = next((u for u in updates if u.get("measurementId") == expected_id and isinstance(u.get("value"), (int, float))), None)
                                                        if isinstance(expected_update, dict):
                                                            _record_measurement_runtime_state(
                                                                measurement_description_catalog,
                                                                source_address=source_address if isinstance(source_address, dict) else None,
                                                                measurement_id=expected_id,
                                                                status="valueAvailable",
                                                                observed_via="targetedReply",
                                                                request_msg_counter=reply_ref,
                                                                reply_header=hdr if isinstance(hdr, dict) else None,
                                                                value=float(expected_update["value"]),
                                                                unit=_unit_to_ha(expected_update.get("unit")),
                                                            )
                                                            logger.info("✅ Targeted measurement ID %s returned a current value", expected_id)
                                                        else:
                                                            raw_entries = _extract_measurement_data_entries(cmd)
                                                            returned_ids = sorted({int(e["measurementId"]) for e in raw_entries if isinstance(e.get("measurementId"), int)})
                                                            _record_measurement_runtime_state(
                                                                measurement_description_catalog,
                                                                source_address=source_address if isinstance(source_address, dict) else None,
                                                                measurement_id=expected_id,
                                                                status="noCurrentValue",
                                                                observed_via="targetedReply",
                                                                request_msg_counter=reply_ref,
                                                                reply_header=hdr if isinstance(hdr, dict) else None,
                                                            )
                                                            logger.info(
                                                                "◻️ Targeted measurement ID %s is advertised but currently returned without a numeric value (returnedIds=%s)",
                                                                expected_id,
                                                                returned_ids,
                                                            )
                                                
                                                # --- RESTORED MQTT PUBLISHING LOOP ---
                                                for u in updates:
                                                    scope = str(u.get("scopeType") or "unknown")
                                                    unit = _unit_to_ha(u.get("unit"))
                                                    mid = u.get("measurementId")
                                                    src = u.get("source") if isinstance(u.get("source"), dict) else {}
                                                    ent = src.get("entity") if isinstance(src, dict) else None
                                                    feat = src.get("feature") if isinstance(src, dict) else None
                                                    source_label = str(src.get("description") or "") if isinstance(src, dict) else ""

                                                    object_id = _slug(f"{scope}_e{'_'.join(str(x) for x in ent) if isinstance(ent, list) else 'na'}_f{feat if isinstance(feat, int) else 'na'}_id{mid}")

                                                    if isinstance(u.get("value"), (int, float)):
                                                        meta = _guess_ha_metadata(scope, unit)
                                                        if object_id not in ha_published:
                                                            friendly_name = _friendly_sensor_name(
                                                                scope,
                                                                source_entity=ent if isinstance(ent, list) else None,
                                                                source_label=source_label,
                                                                measurement_id=mid if isinstance(mid, int) else None,
                                                            )
                                                            mqtt_pub.ensure_discovery(
                                                                object_id=object_id,
                                                                name=friendly_name,
                                                                unit=meta.get("unit", unit),
                                                                device_class=meta.get("device_class", ""),
                                                                state_class=meta.get("state_class", "measurement")
                                                            )
                                                            ha_published.add(object_id)
                                                        mqtt_pub.publish_state(object_id=object_id, value=float(u["value"]))
                                                        logger.info("📡 MQTT published %s = %s", object_id, float(u["value"]))

                                        elif cmd_classifier == "notify":
                                            if "measurementDescriptionListData" in cmd:
                                                source_address = hdr.get("addressSource") if isinstance(hdr, dict) else None
                                                logger.info("📥 Received measurementDescriptionListData notify from %s", source_address)
                                                desc_map = capture_measurement_descriptions(
                                                    cmd,
                                                    source_address=source_address if isinstance(source_address, dict) else None,
                                                    catalog=measurement_description_catalog,
                                                    entity_labels=remote_entity_labels,
                                                    remote_device_address=remote_device_address,
                                                    remote_ski=peer_ski,
                                                    local_ship_id=local_ship_id,
                                                    local_ski=my_ski,
                                                )
                                                key = _desc_key_from_address(source_address)
                                                if key is not None:
                                                    measurement_desc_maps[key] = desc_map

                                                    already_requested = targeted_measurement_ids_requested.setdefault(key, set())
                                                    missing_ids = [
                                                        mid for mid in sorted(desc_map.keys())
                                                        if isinstance(mid, int) and mid >= 0 and mid not in already_requested
                                                    ]
                                                    if missing_ids and remote_device_address is not None:
                                                        logger.info(
                                                            "🎯 Requesting %d newly advertised measurement ID(s) from notify entity=%s feature=%s: %s",
                                                            len(missing_ids),
                                                            list(key[0]),
                                                            key[1],
                                                            missing_ids,
                                                        )
                                                        remote_feature = {"entity": list(key[0]), "feature": int(key[1])}
                                                        for measurement_id in missing_ids:
                                                            request_counter = await request_remote_measurement_id(
                                                                ws,
                                                                local_device_address=local_device_address,
                                                                remote_device_address=remote_device_address,
                                                                remote_measurement_feature=remote_feature,
                                                                measurement_id=measurement_id,
                                                                msg_counter=msg_counter,
                                                            )
                                                            if isinstance(request_counter, int):
                                                                pending_targeted_measurement_reads[request_counter] = {
                                                                    "measurementId": int(measurement_id),
                                                                    "sourceKey": key,
                                                                }
                                                            already_requested.add(measurement_id)

                                            elif "measurementListData" in cmd:
                                                logger.info("📥 Received measurementListData notify from %s", hdr.get("addressSource"))
                                                key = _desc_key_from_address(hdr.get("addressSource"))
                                                desc_map = measurement_desc_maps.get(key, {}) if key is not None else {}
                                                source_address = hdr.get("addressSource") if isinstance(hdr, dict) else None
                                                updates = parse_measurement_list(cmd, desc_map, source_address=source_address if isinstance(source_address, dict) else None, entity_labels=remote_entity_labels)
                                                logger.info("📊 Parsed %d numeric measurement update(s) from notify", len(updates))
                                                _record_observed_measurement_updates(
                                                    measurement_description_catalog,
                                                    updates=updates,
                                                    source_address=source_address if isinstance(source_address, dict) else None,
                                                    observed_via="notify",
                                                    reply_header=hdr if isinstance(hdr, dict) else None,
                                                )
                                                await _maybe_reprobe_compressor_measurements(updates, observed_via="notify")
                                                
                                                # --- RESTORED MQTT PUBLISHING LOOP ---
                                                for u in updates:
                                                    scope = str(u.get("scopeType") or "unknown")
                                                    unit = _unit_to_ha(u.get("unit"))
                                                    mid = u.get("measurementId")
                                                    src = u.get("source") if isinstance(u.get("source"), dict) else {}
                                                    ent = src.get("entity") if isinstance(src, dict) else None
                                                    feat = src.get("feature") if isinstance(src, dict) else None
                                                    source_label = str(src.get("description") or "") if isinstance(src, dict) else ""

                                                    object_id = _slug(f"{scope}_e{'_'.join(str(x) for x in ent) if isinstance(ent, list) else 'na'}_f{feat if isinstance(feat, int) else 'na'}_id{mid}")

                                                    if isinstance(u.get("value"), (int, float)):
                                                        meta = _guess_ha_metadata(scope, unit)
                                                        if object_id not in ha_published:
                                                            friendly_name = _friendly_sensor_name(
                                                                scope,
                                                                source_entity=ent if isinstance(ent, list) else None,
                                                                source_label=source_label,
                                                                measurement_id=mid if isinstance(mid, int) else None,
                                                            )
                                                            mqtt_pub.ensure_discovery(
                                                                object_id=object_id,
                                                                name=friendly_name,
                                                                unit=meta.get("unit", unit),
                                                                device_class=meta.get("device_class", ""),
                                                                state_class=meta.get("state_class", "measurement")
                                                            )
                                                            ha_published.add(object_id)
                                                        mqtt_pub.publish_state(object_id=object_id, value=float(u["value"]))
                                                        logger.info("📡 MQTT published %s = %s", object_id, float(u["value"]))

                                    except Exception as e:
                                        print(f"⚠️  [SPINE] Error processing: {e}")

                                except Exception as e:
                                    print(f"\n📨 Message #{message_count} (SPINE decode error): {e}")
                                    
                    # --- SUBSCRIPTION ---
                    if peer_use_case_received and remote_device_address is not None and selected_measurement_servers:
                        if not measurement_subscription_sent:
                            measurement_subscription_sent = True
                            logger.info("📡 Subscribing to %d remote measurement servers", len(selected_measurement_servers))
                            for server in selected_measurement_servers:
                                await subscribe_remote_measurement(ws, local_device_address=local_device_address, remote_device_address=remote_device_address, remote_measurement_feature=server, msg_counter=msg_counter)
                            logger.info("📤 Measurement subscription request(s) sent")

                            if not polled_once:
                                polled_once = True
                                logger.info("⏱️ Initial poll for all measurement values")
                                for server in selected_measurement_servers:
                                    await request_remote_measurement_once(ws, local_device_address=local_device_address, remote_device_address=remote_device_address, remote_measurement_feature=server, msg_counter=msg_counter)
                                    logger.info("📤 Measurement discovery poll sent for server: entity=%s feature=%s", server.get("entity"), server.get("feature"))
                                    
                                    ent = server.get("entity")
                                    feat = server.get("feature")
                                    
            except PeerIdentityError as exc:
                logger.error("❌ VR921 peer identity verification failed: %s", exc)
                logger.error("   Connection blocked; saved EEBUS identity was not modified")
                await asyncio.sleep(60)
            except Exception as exc:
                close_code = _connection_close_code(exc)
                if close_code == 4452:
                    logger.warning("🔐 WAITING_FOR_TRUST: VR921 rejected this EEBUS node (4452)")
                    logger.warning("   Local SKI: %s", my_ski)
                    logger.warning("   SHIP-ID:   %s", local_ship_id)
                    logger.warning("   Approve this device in myVAILLANT → Settings → Network settings → EEBUS")
                    logger.info("🔄 Retrying trust check in %d seconds...", trust_retry_seconds)
                    await asyncio.sleep(trust_retry_seconds)
                    continue
                if close_code == 4500:
                    logger.warning("🔌 VR921 closed the SHIP session (4500 User close); reconnecting in %d seconds", reconnect_seconds)
                    await asyncio.sleep(reconnect_seconds)
                    continue
                logger.exception("❌ Connection error: %s", exc)
                await asyncio.sleep(60)
            finally:
                try: mqtt_pub.close()
                except Exception: pass
    finally:
        await aiozc.async_unregister_all_services()
        await aiozc.async_close()
        logger.info("👋 Exiting.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Abgebrochen durch Benutzer")
