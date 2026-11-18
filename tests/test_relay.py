import base64
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
import json
from pathlib import Path
import socket
import struct
import subprocess
import sys
import unittest
from unittest.mock import patch

EXECUTABLE = Path(__file__).resolve().parents[1] / "dvmtrstream-pro"
loader = SourceFileLoader("dvmtrstream_pro", str(EXECUTABLE))
relay = module_from_spec(spec_from_loader(loader.name, loader))
sys.modules[loader.name] = relay
loader.exec_module(relay)

AUDIO = b"\x12\x34" * 160


class FakeSocket:
    def __init__(self):
        self.packets = []

    def sendto(self, data, address):
        self.packets.append(data)
        return len(data)


class EndpointTests(unittest.TestCase):
    def endpoint(self, **settings):
        timing = {name: schema["default"] for name, schema in relay.TIMING_PROPERTIES.items()}
        return relay.Endpoint(FakeSocket(), ("127.0.0.1", 32001), timing | settings)

    def test_serializes_overlapping_calls_and_holds_active_across_gaps(self):
        endpoint = self.endpoint(silenceLeader=20, interCallDelay=60)
        endpoint.offer("a", AUDIO, (100, 1), 1.0)
        endpoint.offer("b", AUDIO, (200, 2), 1.0)
        for now in (1.0, 1.02, 1.04, 1.06):
            endpoint.tick(now)
        self.assertEqual(endpoint.socket.packets, [
            relay.packet(relay.SILENCE, (100, 1)), relay.packet(AUDIO, (100, 1)),
        ])

        endpoint.offer("a", AUDIO[:100], (100, 1), 1.08)
        endpoint.end("a")
        endpoint.tick(1.08)
        endpoint.tick(1.10)
        endpoint.tick(1.14)
        self.assertEqual(len(endpoint.socket.packets), 3)
        endpoint.tick(1.18)
        endpoint.tick(1.20)
        self.assertEqual(endpoint.socket.packets[2:], [
            relay.packet(AUDIO[:100], (100, 1)),
            relay.packet(relay.SILENCE, (200, 2)),
            relay.packet(AUDIO, (200, 2)),
        ])

    def test_pcm_fragments_join_and_ids_change_at_frame_boundary(self):
        endpoint = self.endpoint()
        endpoint.offer("a", AUDIO[:100], (100, 1), 1.0)
        endpoint.offer("a", AUDIO[100:] + AUDIO[:60], (100, 1), 1.01)
        endpoint.offer("a", AUDIO, (100, 2), 1.02)
        endpoint.tick(1.02)
        endpoint.tick(1.04)
        endpoint.tick(1.06)
        self.assertEqual(endpoint.socket.packets, [
            relay.packet(AUDIO, (100, 1)),
            relay.packet(AUDIO[:60], (100, 1)),
            relay.packet(AUDIO, (100, 2)),
        ])

    def test_drops_expired_and_overflow_frames(self):
        endpoint = self.endpoint(maxQueueMs=100, callGapHoldMs=200)
        endpoint.offer("a", AUDIO * 7, (100, 1), 1.0)
        self.assertEqual(endpoint.queued, 5)
        self.assertEqual(endpoint.dropped, 2)
        endpoint.tick(1.12)
        self.assertEqual(endpoint.queued, 0)
        self.assertEqual(endpoint.dropped, 7)
        self.assertEqual(endpoint.socket.packets, [])

    def test_stale_active_call_releases_without_end_event(self):
        endpoint = self.endpoint(callGapHoldMs=100, interCallDelay=0)
        endpoint.offer("a", AUDIO, (100, 1), 1.0)
        endpoint.tick(1.0)
        endpoint.offer("b", AUDIO, (200, 2), 1.02)
        endpoint.tick(1.08)
        self.assertEqual(len(endpoint.socket.packets), 1)
        endpoint.tick(1.11)
        self.assertEqual(endpoint.socket.packets[-1], relay.packet(AUDIO, (200, 2)))

    def test_uses_system_index_and_unique_site_twin_for_patch_metadata(self):
        endpoint = self.endpoint()
        endpoint.routes.append(relay.Route("county", 200, None, None))
        worker = relay.Relay([endpoint], [{"index": 3, "short_name": "county"}])
        worker.event({
            "type": "call.start", "id": 5, "system": 3, "talkgroup": 100,
            "patched_talkgroups": [100, 200], "sources": [{"unit": 41}],
        })
        worker.event({
            "type": "call.update", "id": 5, "system": 3, "talkgroup": 100,
            "patched_talkgroups": [100, 200], "sources": [{"unit": 49}],
        })
        with patch.object(relay.time, "monotonic", return_value=1.0):
            worker.event(audio(6, 100, system=3, short_name=""))
        worker.event({"type": "call.end", "id": 5, "system": 3})
        endpoint.tick(1.0)
        endpoint.tick(1.02)
        self.assertEqual(endpoint.socket.packets, [relay.packet(AUDIO, (100, 49))])
        self.assertIsNone(endpoint.active)


def audio(call_id, talkgroup, *, system=0, short_name="county"):
    return {
        "type": "audio", "call_id": call_id, "short_name": short_name, "system": system,
        "talkgroup": talkgroup, "sample_rate": 8000,
        "pcm": base64.b64encode(AUDIO).decode(),
    }


class ProtocolTests(unittest.TestCase):
    def test_invalid_configuration_exits_with_status_78(self):
        hello = {"type": "hello", "api": 1, "config": {"streams": [
            {"TGID": 100, "address": "127.0.0.1", "port": 0},
        ]}}
        result = subprocess.run([str(EXECUTABLE)], input=json.dumps(hello) + "\n",
                                capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 78)
        self.assertEqual(json.loads(result.stdout)["state"], "error")
        self.assertIn("Stream 1: port", result.stdout)

    def test_socket_fanout_deduplication_and_wire_format(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as first, \
             socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as second:
            for receiver in (first, second):
                receiver.bind(("127.0.0.1", 0))
                receiver.settimeout(3)
            first_port = first.getsockname()[1]
            second_port = second.getsockname()[1]
            config = {"streams": [
                {"shortName": "county", "TGID": 200, "address": "127.0.0.1", "port": first_port,
                 "destinationId": 0x123456},
                {"TGID": 0, "address": "127.0.0.1", "port": first_port, "destinationId": 99},
                {"TGID": 100, "address": "127.0.0.1", "port": second_port, "sourceId": 9},
            ]}
            with subprocess.Popen([str(EXECUTABLE)], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
                try:
                    messages = [
                        {"type": "hello", "api": 1, "config": config},
                        {"type": "call.start", "id": 7, "short_name": "county",
                         "talkgroup": 100, "patched_talkgroups": [100, 200],
                         "sources": [{"unit": 0x12345678}]},
                        audio(7, 100),
                        {"type": "call.end", "id": 7, "short_name": "county"},
                    ]
                    process.stdin.write("".join(json.dumps(m) + "\n" for m in messages))
                    process.stdin.flush()
                    self.assertEqual(first.recv(1024),
                                     b"\x00\x00\x01\x40" + AUDIO + b"\x00\x12\x34\x56\x00\x34\x56\x78")
                    received = second.recv(1024)
                    self.assertEqual(struct.unpack_from(">II", received, 324), (100, 9))
                    first.settimeout(0.1)
                    with self.assertRaises(socket.timeout):
                        first.recv(1024)
                    stdout, stderr = process.communicate('{"type":"shutdown","grace_s":1}\n', timeout=3)
                    self.assertEqual(process.returncode, 0, stderr)
                    self.assertEqual(json.loads(stdout.splitlines()[0]), {"type": "ready"})
                    self.assertTrue(any(json.loads(line)["type"] == "metrics" for line in stdout.splitlines()))
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.communicate(timeout=3)


if __name__ == "__main__":
    unittest.main()
