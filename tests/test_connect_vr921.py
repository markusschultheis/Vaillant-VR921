import asyncio
import json
import os
import stat
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization

import connect_vr921 as vr


@contextmanager
def working_directory(path: str):
    previous = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(data)


class FakeHandshakeWebSocket(FakeWebSocket):
    def __init__(self, received):
        super().__init__()
        self.received = list(received)

    async def recv(self):
        return self.received.pop(0)


class FakeMqttPublisher:
    def ensure_discovery(self, **kwargs):
        pass

    def publish_state(self, **kwargs):
        pass


class CodecTests(unittest.TestCase):
    def round_trip(self, payload):
        encoded = vr.json_into_eebus_json(payload)
        return json.loads(vr.json_from_eebus_json(encoded))

    def test_round_trip_preserves_strings_and_real_arrays(self):
        payload = {
            "literal": "text },{ and [{ must stay unchanged",
            "items": [{"a": 1}, {"b": 2}],
            "empty": [],
            "scaled": {"number": 123, "scale": -1},
        }
        self.assertEqual(payload, self.round_trip(payload))

    def test_parser_returns_every_command(self):
        message = {
            "data": {
                "payload": {
                    "datagram": {
                        "header": {"cmdClassifier": "reply"},
                        "payload": {
                            "cmd": [
                                {"measurementListData": []},
                                {"setpointListData": []},
                            ]
                        },
                    }
                }
            }
        }
        header, commands = vr._parse_spine_datagram(message)
        self.assertEqual("reply", header["cmdClassifier"])
        self.assertEqual(2, len(commands))

    def test_ship_data_frame_round_trip(self):
        websocket = FakeWebSocket()
        datagram = {
            "datagram": {
                "header": {"cmdClassifier": "read", "msgCounter": 7},
                "payload": {
                    "cmd": [
                        {"measurementListData": {}},
                        {"setpointListData": {}},
                    ]
                },
            }
        }
        asyncio.run(vr.send_ship_data(websocket, datagram))
        frame = websocket.sent[0]
        self.assertEqual(0x02, frame[0])
        decoded = json.loads(vr.json_from_eebus_json(frame[1:].decode("utf-8")))
        _, commands = vr._parse_spine_datagram(decoded)
        self.assertEqual(2, len(commands))


class DiscoveryTests(unittest.TestCase):
    def test_preserves_duplicate_feature_types_and_filters_read_operations(self):
        discovery = {
            "featureInformation": [
                {
                    "description": {
                        "featureAddress": {"entity": [3, 1], "feature": 11},
                        "featureType": "Measurement",
                        "role": "server",
                        "supportedFunction": [
                            {
                                "function": "measurementListData",
                                "possibleOperations": {"read": {}},
                            },
                            {
                                "function": "unknownWriteData",
                                "possibleOperations": {"write": {}},
                            },
                        ],
                    }
                },
                {
                    "description": {
                        "featureAddress": {"entity": [4], "feature": 11},
                        "featureType": "Measurement",
                        "role": "server",
                        "supportedFunction": [
                            {
                                "function": "measurementDescriptionListData",
                                "possibleOperations": {"read": {}},
                            }
                        ],
                    }
                },
            ]
        }
        capabilities = vr._extract_feature_capabilities(discovery)
        self.assertEqual(2, len(capabilities))
        self.assertEqual(((3, 1), 11), capabilities[0].key)
        self.assertEqual(("measurementListData",), capabilities[0].readable_functions())
        self.assertEqual(((4,), 11), capabilities[1].key)

    def test_local_discovery_advertises_read_only_client_families(self):
        discovery = vr.build_local_detailed_discovery("local-device")
        types = {
            item["description"]["featureType"]
            for item in discovery["featureInformation"]
            if item["description"].get("role") == "client"
        }
        self.assertTrue({"Measurement", "Setpoint", "HVAC", "SmartEnergyManagementPs"}.issubset(types))

    def test_read_plan_requires_server_role_and_advertised_read_operation(self):
        websocket = FakeWebSocket()
        runtime = vr.SpineRuntime(
            websocket,
            local_device_address="local",
            msg_counter=vr.MsgCounter(),
            mqtt_pub=FakeMqttPublisher(),
            publish_jsonl=False,
            discovery_log=False,
        )
        runtime.subscribe_updates = False
        runtime.remote_device_address = "remote"
        runtime.discovery_received = True
        runtime.use_case_received = True
        runtime.capabilities = [
            vr.FeatureCapability(
                entity=(3,),
                feature=11,
                feature_type="Measurement",
                role="server",
                operations_by_function={"measurementListData": frozenset({"read"})},
            ),
            vr.FeatureCapability(
                entity=(4,),
                feature=11,
                feature_type="Measurement",
                role="client",
                operations_by_function={"measurementListData": frozenset({"read"})},
            ),
            vr.FeatureCapability(
                entity=(5,),
                feature=18,
                feature_type="Setpoint",
                role="server",
                operations_by_function={"setpointListData": frozenset({"write"})},
            ),
        ]

        asyncio.run(runtime._start_read_plan())

        self.assertEqual(1, len(websocket.sent))
        decoded = json.loads(vr.json_from_eebus_json(websocket.sent[0][1:].decode("utf-8")))
        _, commands = vr._parse_spine_datagram(decoded)
        self.assertEqual([{"measurementListData": []}], commands)


class RuntimeTests(unittest.TestCase):
    def make_runtime(self, websocket=None):
        return vr.SpineRuntime(
            websocket or FakeWebSocket(),
            local_device_address="local",
            msg_counter=vr.MsgCounter(),
            mqtt_pub=FakeMqttPublisher(),
            publish_jsonl=False,
            discovery_log=False,
        )

    def test_read_stays_pending_after_ack_until_reply(self):
        runtime = self.make_runtime()
        runtime._remember_request(42, function="measurementListData", target=((3,), 11))

        runtime._handle_result(
            {"msgCounterReference": 42},
            [{"resultData": {"errorNumber": 0}}],
        )
        self.assertTrue(runtime.pending[42].ack_received)

        runtime._complete_reply({"msgCounterReference": 42})
        self.assertNotIn(42, runtime.pending)

    def test_optional_use_case_timeout_does_not_block_read_plan(self):
        runtime = self.make_runtime()
        runtime.use_case_received = False
        runtime._remember_request(7, function="nodeManagementUseCaseData", target=((0,), 0))
        runtime.pending[7].sent_at = 0.0

        runtime._expire_requests()

        self.assertTrue(runtime.use_case_received)
        self.assertNotIn(7, runtime.pending)

    def test_result_ack_is_sent_only_when_requested(self):
        websocket = FakeWebSocket()
        runtime = self.make_runtime(websocket)
        runtime.remote_device_address = "remote"
        runtime.discovery_requested = True
        runtime.read_plan_started = True
        base_header = {
            "specificationVersion": "1.3.0",
            "addressSource": {"device": "remote", "entity": [3], "feature": 11},
            "addressDestination": {"device": "local", "entity": [1], "feature": 1},
            "msgCounter": 10,
            "cmdClassifier": "notify",
        }

        asyncio.run(runtime.handle_datagram(dict(base_header), [{"measurementListData": []}]))
        self.assertEqual([], websocket.sent)

        asyncio.run(
            runtime.handle_datagram(
                {**base_header, "msgCounter": 11, "ackRequest": True},
                [{"measurementListData": []}],
            )
        )
        self.assertEqual(1, len(websocket.sent))


class HandshakeTests(unittest.TestCase):
    @staticmethod
    def control(payload):
        return b"\x01" + vr.json_into_eebus_json(payload).encode("utf-8")

    def test_validated_handshake_completes(self):
        websocket = FakeHandshakeWebSocket(
            [
                b"\x00\x00",
                self.control({"connectionHello": {"phase": "ready"}}),
                self.control(
                    {
                        "messageProtocolHandshake": {
                            "handshakeType": "select",
                            "version": {"major": 1, "minor": 0},
                            "formats": {"format": ["JSON-UTF8"]},
                        }
                    }
                ),
                self.control({"connectionPinState": {"pinState": "none"}}),
                self.control({"accessMethodsRequest": {}}),
                self.control({"accessMethods": {"id": "vr921"}}),
            ]
        )

        self.assertTrue(asyncio.run(vr.perform_ship_handshake(websocket, "local-ship-id")))
        self.assertGreaterEqual(len(websocket.sent), 5)

    def test_unexpected_cmi_is_rejected(self):
        websocket = FakeHandshakeWebSocket([b"\x00\x01"])
        self.assertFalse(asyncio.run(vr.perform_ship_handshake(websocket, "local-ship-id")))


class DataParserTests(unittest.TestCase):
    def test_measurement_metadata_and_quality_are_preserved(self):
        descriptions = vr.parse_measurement_description(
            {
                "measurementDescriptionListData": {
                    "measurementDescriptionData": [
                        {
                            "measurementId": 4,
                            "measurementType": "temperature",
                            "commodityType": "water",
                            "unit": "degC",
                            "scopeType": "dhwTemperature",
                            "label": "DHW",
                        }
                    ]
                }
            }
        )
        updates = vr.parse_measurement_list(
            {
                "measurementListData": {
                    "measurementData": [
                        {
                            "measurementId": 4,
                            "value": {"number": 487, "scale": -1},
                            "valueType": "value",
                            "valueSource": "measuredValue",
                            "valueTendency": "stable",
                            "valueState": "normal",
                            "timestamp": {"dateTime": "2026-08-20T12:00:00Z"},
                        }
                    ]
                }
            },
            descriptions,
            source_address={"entity": [4], "feature": 11},
        )
        self.assertEqual(48.7, updates[0]["value"])
        self.assertEqual("water", updates[0]["commodityType"])
        self.assertEqual("measuredValue", updates[0]["valueSource"])
        self.assertEqual("normal", updates[0]["valueState"])

    def test_setpoint_metadata_limits_and_state_are_preserved(self):
        descriptions = vr.parse_setpoint_description(
            {
                "setpointDescriptionListData": {
                    "setpointDescriptionData": [
                        {
                            "setpointId": 1,
                            "scopeType": "dhwTemperature",
                            "unit": "degC",
                            "setpointType": "valueAbsolute",
                        }
                    ]
                }
            }
        )
        updates = vr.parse_setpoint_list(
            {
                "setpointListData": {
                    "setpointData": [
                        {
                            "setpointId": 1,
                            "value": {"number": 50, "scale": 0},
                            "valueMin": {"number": 35, "scale": 0},
                            "valueMax": {"number": 70, "scale": 0},
                            "isSetpointChangeable": True,
                            "isSetpointActive": True,
                        }
                    ]
                }
            },
            descriptions,
            source_address={"entity": [4], "feature": 18},
        )
        self.assertEqual(50.0, updates[0]["value"])
        self.assertEqual(35.0, updates[0]["valueMin"])
        self.assertEqual(70.0, updates[0]["valueMax"])
        self.assertEqual("°C", updates[0]["unit"])


class IdentityTests(unittest.TestCase):
    def test_private_key_and_peer_pin_are_mode_0600(self):
        with tempfile.TemporaryDirectory() as directory, working_directory(directory):
            ski = vr.get_or_create_certificate()
            self.assertEqual(ski, vr.get_or_create_certificate())
            self.assertEqual(40, len(ski))
            self.assertEqual(0o600, stat.S_IMODE(os.stat("key.pem").st_mode))
            self.assertEqual(0o644, stat.S_IMODE(os.stat("cert.pem").st_mode))

            pin_path = Path("peer.json")
            vr._store_peer_pin(pin_path, ski=ski, fingerprint="ab" * 32)
            self.assertEqual(0o600, stat.S_IMODE(pin_path.stat().st_mode))
            self.assertEqual(ski, vr._load_peer_pin(pin_path)["ski"])

            with self.assertRaises(ValueError):
                vr._normalize_ski("x" + ski)
            with self.assertRaises(ValueError):
                vr._normalize_sha256_fingerprint("not-a-fingerprint")

    def test_tls_certificate_must_match_advertised_and_pinned_identity(self):
        with tempfile.TemporaryDirectory() as directory, working_directory(directory):
            ski = vr.get_or_create_certificate()
            certificate = x509.load_pem_x509_certificate(Path("cert.pem").read_bytes())
            der = certificate.public_bytes(serialization.Encoding.DER)

            class FakeSSLObject:
                def getpeercert(self, binary_form=False):
                    return der if binary_form else {}

            class FakeTransport:
                def get_extra_info(self, name):
                    return FakeSSLObject() if name == "ssl_object" else None

            class FakeTLSWebSocket:
                transport = FakeTransport()

            peer_ski, fingerprint = vr._verify_peer_certificate(
                FakeTLSWebSocket(),
                advertised_ski=ski,
                pinned={"ski": ski},
            )
            self.assertEqual(ski, peer_ski)
            self.assertEqual(64, len(fingerprint))
            with self.assertRaises(vr.PeerIdentityError):
                vr._verify_peer_certificate(
                    FakeTLSWebSocket(),
                    advertised_ski="00" * 20,
                    pinned={},
                )


if __name__ == "__main__":
    unittest.main()
