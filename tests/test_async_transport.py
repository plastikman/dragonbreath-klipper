"""DragonBreath API v2 transport tests (stdlib only)."""

import json
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dragonbreath  # noqa: E402


def _wait_until(pred, timeout=8.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return pred()


class _MockDevice:
    def __init__(self):
        self.command_delay = 0.0
        self.state_delay = 0.0
        self.command_fail_times = 0
        self.external_after_command_failure = False
        self.reject_next_power_on = False
        self.lease_counter = 0
        self._requests = []
        self._lock = threading.Lock()
        self.state = self._new_state()

    def _new_state(self):
        return {
            "api_version": 2,
            "device_id": "dragonbreath-test",
            "boot_id": "0123456789abcdef0123456789abcdef",
            "firmware": "test",
            "state_revision": 1,
            "mode": "off",
            "source": "boot",
            "target": {
                "requested_c": 0.0,
                "effective_c": 0.0,
                "maximum_c": 70.0,
            },
            "heater": {"demand": False, "output": False},
            "fan": {
                "requested_percent": 100,
                "effective_percent": 0,
                "reason": "off",
            },
            "sensors": {
                "chamber": {"temperature_c": 24.0, "status": "ok"},
                "ptc": {"temperature_c": 25.0, "status": "ok"},
            },
            "environment": {
                "moonraker_connected": True,
                "bed_temperature_c": 60.0,
                "auto_engaged": False,
                "auto_bed_threshold_c": 100.0,
            },
            "drying": {"active": False, "remaining_seconds": 0},
            "control": {
                "lease": {
                    "active": False,
                    "id": None,
                    "owner": None,
                    "expires_in_ms": 0,
                }
            },
            "safety": {
                "fault_latched": False,
                "inhibited": False,
                "reason": None,
            },
        }

    def record(self, method, path, body=None):
        with self._lock:
            self._requests.append((method, path, body))

    def requests(self, method=None, path=None):
        with self._lock:
            return [
                item for item in self._requests
                if (method is None or item[0] == method)
                and (path is None or item[1] == path)
            ]

    def snapshot(self):
        with self._lock:
            return json.loads(json.dumps(self.state))

    def external_off(self, source="button"):
        with self._lock:
            self.state["state_revision"] += 1
            self.state["mode"] = "off"
            self.state["source"] = source
            self.state["target"]["requested_c"] = 0.0
            self.state["target"]["effective_c"] = 0.0
            self.state["heater"] = {"demand": False, "output": False}
            self.state["control"]["lease"] = {
                "active": False, "id": None, "owner": None, "expires_in_ms": 0,
            }

    def handle_command(self, body):
        with self._lock:
            if self.command_fail_times:
                self.command_fail_times -= 1
                if self.external_after_command_failure:
                    self.external_after_command_failure = False
                    self.external_off_locked("button")
                return 500, {"error": "temporary"}
            command = body["command"]["name"]
            if command == "power_on" and self.reject_next_power_on:
                self.reject_next_power_on = False
                self.external_off_locked("button")
                return 409, {
                    "ok": False,
                    "error": "revision_conflict",
                    "state": json.loads(json.dumps(self.state)),
                }
            if command != "off" and body.get("expected_revision") != \
                    self.state["state_revision"]:
                return 409, {
                    "ok": False,
                    "error": "revision_conflict",
                    "state": json.loads(json.dumps(self.state)),
                }
            self.state["state_revision"] += 1
            self.state["source"] = "klipper"
            if command == "power_on":
                target = float(body["command"]["target_c"])
                self.lease_counter += 1
                lease_id = ("%032x" % self.lease_counter)[-32:]
                self.state["mode"] = "power_on"
                self.state["target"]["requested_c"] = target
                self.state["target"]["effective_c"] = target
                self.state["heater"] = {"demand": True, "output": True}
                self.state["control"]["lease"] = {
                    "active": True,
                    "id": lease_id,
                    "owner": body["actor"]["id"],
                    "expires_in_ms": 300000,
                }
            else:
                self.external_off_locked("klipper", increment=False)
            return 200, {
                "ok": True,
                "request_id": body["request_id"],
                "state": json.loads(json.dumps(self.state)),
            }

    def external_off_locked(self, source, increment=True):
        if increment:
            self.state["state_revision"] += 1
        self.state["mode"] = "off"
        self.state["source"] = source
        self.state["target"]["requested_c"] = 0.0
        self.state["target"]["effective_c"] = 0.0
        self.state["heater"] = {"demand": False, "output": False}
        self.state["control"]["lease"] = {
            "active": False, "id": None, "owner": None, "expires_in_ms": 0,
        }


def _make_handler(dev):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _json(self, status, body):
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            dev.record("GET", self.path)
            if self.path == "/api/v2/info":
                self._json(200, {
                    "api_version": 2,
                    "device_id": "dragonbreath-test",
                    "boot_id": dev.state["boot_id"],
                    "firmware": "test",
                })
            elif self.path == "/api/v2/state":
                if dev.state_delay:
                    time.sleep(dev.state_delay)
                self._json(200, dev.snapshot())
            elif self.path == "/api/v2/events":
                # Force the transport's documented serialized polling fallback.
                self._json(503, {"error": "busy"})
            else:
                self.send_error(404)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
            dev.record("POST", self.path, body)
            if self.path == "/api/v2/command":
                if dev.command_delay:
                    time.sleep(dev.command_delay)
                status, response = dev.handle_command(body)
                self._json(status, response)
            elif self.path == "/api/v2/heartbeat":
                state = dev.snapshot()
                lease = state["control"]["lease"]
                if lease["active"] and body.get("lease_id") == lease["id"]:
                    self._json(200, {
                        "ok": True,
                        "state_revision": state["state_revision"],
                        "expires_in_ms": 300000,
                    })
                else:
                    self._json(409, {
                        "ok": False,
                        "error": "stale_lease",
                        "state": state,
                    })
            else:
                self.send_error(404)

    return Handler


class ApiV2TransportTest(unittest.TestCase):
    def setUp(self):
        self.dev = _MockDevice()
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), _make_handler(self.dev))
        self.host, self.port = self.server.server_address
        self.server_thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.transports = []

    def tearDown(self):
        for transport in self.transports:
            transport.stop()
        self.server.shutdown()
        self.server.server_close()

    def transport(self, poll=0.05, on_message=None, on_disconnect=None):
        transport = dragonbreath._DragonBreathHTTP(
            self.host, self.port, "web",
            on_message or (lambda state: None),
            on_disconnect or (lambda: None),
            poll)
        self.transports.append(transport)
        return transport

    def wait_initial_off(self, transport):
        transport.start()
        self.assertTrue(_wait_until(
            lambda: transport._applied_target == 0.0, timeout=5.0))

    def power_commands(self):
        return [
            body for _, _, body in
            self.dev.requests("POST", "/api/v2/command")
            if body["command"]["name"] == "power_on"
        ]

    def test_set_target_is_nonblocking(self):
        transport = self.transport()
        self.wait_initial_off(transport)
        self.dev.command_delay = 3.0
        started = time.monotonic()
        transport.set_target(60.0)
        self.assertLess(time.monotonic() - started, 0.05)
        self.assertTrue(_wait_until(
            lambda: bool(self.power_commands()), timeout=5.0))

    def test_state_is_polled_and_delivered(self):
        messages = []
        transport = self.transport(on_message=messages.append)
        transport.start()
        self.assertTrue(_wait_until(
            lambda: any(m.get("temperature") == 24.0 for m in messages),
            timeout=5.0))

    def test_retry_reuses_request_id(self):
        transport = self.transport()
        self.wait_initial_off(transport)
        self.dev.command_fail_times = 3
        transport.set_target(45.0)
        self.assertTrue(_wait_until(
            lambda: transport._applied_target == 45.0, timeout=8.0))
        commands = self.power_commands()
        self.assertGreaterEqual(len(commands), 4)
        self.assertEqual(1, len({body["request_id"] for body in commands}))

    def test_heartbeat_carries_exact_device_lease(self):
        transport = self.transport()
        self.wait_initial_off(transport)
        transport.set_target(50.0)
        self.assertTrue(_wait_until(
            lambda: bool(self.dev.requests(
                "POST", "/api/v2/heartbeat")), timeout=5.0))
        heartbeat = self.dev.requests(
            "POST", "/api/v2/heartbeat")[-1][2]
        state = self.dev.snapshot()
        self.assertEqual(
            state["control"]["lease"]["id"], heartbeat["lease_id"])
        self.assertEqual(32, len(heartbeat["lease_id"]))

    def test_external_off_invalidates_ownership_without_rearm(self):
        messages = []
        transport = self.transport(on_message=messages.append)
        self.wait_initial_off(transport)
        transport.set_target(50.0)
        self.assertTrue(_wait_until(
            lambda: transport._lease_id is not None, timeout=5.0))
        before = len(self.power_commands())
        self.dev.external_off("button")
        self.assertTrue(_wait_until(
            lambda: any(m.get("countermanded") for m in messages),
            timeout=5.0))
        time.sleep(0.3)
        self.assertIsNone(transport._lease_id)
        self.assertEqual(0.0, transport._desired_target)
        self.assertEqual(before, len(self.power_commands()))

    def test_revision_conflict_is_terminal_not_blind_retry(self):
        messages = []
        transport = self.transport(on_message=messages.append)
        self.wait_initial_off(transport)
        self.dev.reject_next_power_on = True
        transport.set_target(55.0)
        self.assertTrue(_wait_until(
            lambda: any(m.get("protocol_error") == "revision_conflict"
                        for m in messages), timeout=5.0))
        time.sleep(0.3)
        self.assertEqual(1, len(self.power_commands()))
        self.assertEqual(0.0, transport._desired_target)

    def test_retry_keeps_original_expected_revision(self):
        messages = []
        transport = self.transport(on_message=messages.append)
        self.wait_initial_off(transport)
        self.dev.command_fail_times = 1
        self.dev.external_after_command_failure = True
        transport.set_target(52.0)
        self.assertTrue(_wait_until(
            lambda: any(m.get("protocol_error") == "revision_conflict"
                        for m in messages), timeout=5.0))
        commands = self.power_commands()
        self.assertGreaterEqual(len(commands), 2)
        self.assertEqual(
            1, len({body["expected_revision"] for body in commands}))
        self.assertEqual(
            1, len({body["request_id"] for body in commands}))

    def test_force_off_is_unconditional_v2_command(self):
        transport = self.transport()
        self.wait_initial_off(transport)
        before = len(self.dev.requests("POST", "/api/v2/command"))
        transport.force_off()
        self.assertTrue(_wait_until(
            lambda: len(self.dev.requests("POST", "/api/v2/command")) > before,
            timeout=5.0))
        body = self.dev.requests("POST", "/api/v2/command")[-1][2]
        self.assertEqual("off", body["command"]["name"])

    def test_fault_clear_is_queued_not_blocking(self):
        transport = self.transport()
        self.wait_initial_off(transport)
        started = time.monotonic()
        transport.reset_fault()
        self.assertLess(time.monotonic() - started, 0.05)
        self.assertTrue(_wait_until(
            lambda: any(
                body["command"]["name"] == "clear_fault"
                for _, _, body in self.dev.requests(
                    "POST", "/api/v2/command")),
            timeout=5.0))

    def test_never_calls_alpha_routes(self):
        transport = self.transport()
        self.wait_initial_off(transport)
        transport.set_target(45.0)
        self.assertTrue(_wait_until(
            lambda: transport._applied_target == 45.0, timeout=5.0))
        paths = [path for _, path, _ in self.dev.requests()]
        self.assertFalse(any(
            path in ("/status", "/target", "/heartbeat", "/reset")
            for path in paths))

    def test_pwm_rising_edge_does_not_create_a_new_target_intent(self):
        class Module:
            target = 45.0
            _external_off_lockout = False

            def __init__(self):
                self.calls = []

            def set_device_target(self, target):
                self.calls.append(target)

        module = Module()
        pin = dragonbreath.DragonBreathVirtualPin(module)
        pin._lookup_heater_target = lambda: 45.0
        pin.last_value = 0.0
        pin.set_pwm(0.0, 1.0)
        self.assertEqual([], module.calls)


if __name__ == "__main__":
    unittest.main(verbosity=2)
