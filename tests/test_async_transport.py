# Tests for the DragonBreath HTTP transport's threading contract.
#
# The bug these guard against: control writes used to run synchronous HTTP on
# Klipper's reactor thread, so a single SET_HEATER_TEMPERATURE could stall the
# reactor for seconds and trip "Timer Too Close" mid-print. The transport must
# now do ALL network I/O on its worker thread; the reactor-facing calls only
# hand off desired state and must return immediately.
#
# No external deps — stdlib http.server stands up a controllable fake device.
# Run:  python3 -m pytest tests/         (or)  python3 tests/test_async_transport.py

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
    """Controllable fake DragonBreath HTTP device."""

    def __init__(self):
        self.status_body = {"temp": 24.0, "target": 0.0, "heating": False,
                            "fault": False, "fault_reason": None}
        self.target_delay = 0.0     # seconds the /target handler sleeps
        self.status_delay = 0.0     # seconds the /status handler sleeps
        self.target_fail_times = 0  # next N /target POSTs answer 500
        self.reflect_target = True  # if True, an accepted /target updates status
        self._requests = []         # (method, path)
        self._lock = threading.Lock()

    def record(self, method, path):
        with self._lock:
            self._requests.append((method, path))

    def paths(self, method=None):
        with self._lock:
            return [p for (m, p) in self._requests
                    if method is None or m == method]

    def next_target_fails(self):
        with self._lock:
            if self.target_fail_times > 0:
                self.target_fail_times -= 1
                return True
        return False


def _make_handler(dev):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            dev.record("GET", self.path)
            if self.path.startswith("/status"):
                if dev.status_delay:
                    time.sleep(dev.status_delay)
                body = json.dumps(dev.status_body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404)

        def do_POST(self):
            dev.record("POST", self.path)
            if self.path.startswith("/target"):
                if dev.target_delay:
                    time.sleep(dev.target_delay)
                # A rejected write must NOT change device state (as a real device):
                # decide failure before applying the target.
                if dev.next_target_fails():
                    self.send_error(500)
                    return
                if dev.reflect_target:
                    try:
                        t = float(self.path.split("t=", 1)[1])
                        with dev._lock:
                            dev.status_body["target"] = t
                            dev.status_body["heating"] = t > 0
                    except (IndexError, ValueError):
                        pass
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

    return Handler


class AsyncTransportTest(unittest.TestCase):
    def setUp(self):
        self.dev = _MockDevice()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self.dev))
        self.host, self.port = self.server.server_address
        self._srv_thread = threading.Thread(target=self.server.serve_forever,
                                             daemon=True)
        self._srv_thread.start()
        self._transports = []

    def tearDown(self):
        for t in self._transports:
            t.stop()
        self.server.shutdown()
        self.server.server_close()

    def _transport(self, poll=0.05, on_message=None, on_disconnect=None):
        t = dragonbreath._DragonBreathHTTP(
            self.host, self.port, "web",
            on_message or (lambda s: None),
            on_disconnect or (lambda: None),
            poll)
        self._transports.append(t)
        return t

    def test_set_target_is_nonblocking(self):
        # The regression guard: even with a 3s server-side stall on /target, the
        # reactor-facing set_target() must return effectively instantly. Against
        # the old synchronous code this call blocked for the full ~3s.
        self.dev.target_delay = 3.0
        t = self._transport()
        t.start()
        start = time.monotonic()
        t.set_target(60.0)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 0.05,
                        "set_target blocked the caller for %.3fs" % elapsed)
        # ...and the worker still delivers it despite the slow handler.
        self.assertTrue(
            _wait_until(lambda: "/target?t=60" in self.dev.paths("POST"),
                        timeout=8.0),
            "worker never POSTed the target")

    def test_status_is_polled_and_delivered(self):
        msgs = []
        t = self._transport(on_message=msgs.append)
        t.start()
        self.assertTrue(_wait_until(lambda: len(msgs) > 0, timeout=5.0),
                        "no status delivered")
        self.assertEqual(msgs[0]["temperature"], 24.0)

    def test_write_retries_with_backoff_until_accepted(self):
        # First three /target writes fail (500); the worker must keep retrying
        # until the device accepts — this is the self-healing async OFF path.
        self.dev.target_fail_times = 3
        t = self._transport()
        t.start()
        t.set_target(45.0)
        self.assertTrue(_wait_until(lambda: t._last_sent_target == 45.0, timeout=8.0),
                        "write never succeeded after retries")
        target_posts = [p for p in self.dev.paths("POST") if p.startswith("/target")]
        self.assertGreaterEqual(len(target_posts), 4,
                                "expected retries, saw %r" % target_posts)

    def test_heartbeat_only_while_target_positive(self):
        t = self._transport()
        t.start()
        # No target yet -> no heartbeats.
        time.sleep(0.3)
        self.assertNotIn("/heartbeat", self.dev.paths("POST"))
        # Positive target -> heartbeats start.
        t.set_target(50.0)
        self.assertTrue(_wait_until(lambda: "/heartbeat" in self.dev.paths("POST"),
                                    timeout=5.0),
                        "no heartbeat while heating")

    def test_stop_sends_final_off(self):
        t = self._transport()
        t.start()
        t.set_target(50.0)
        self.assertTrue(_wait_until(lambda: t._last_sent_target == 50.0, timeout=5.0))
        t.stop()
        self.assertTrue(
            _wait_until(lambda: "/target?t=0" in self.dev.paths("POST"), timeout=3.0),
            "stop() did not deliver a final OFF")

    def test_off_asserted_when_device_reports_external_heating(self):
        # Regression (safety): desired target is 0, but the device reports it is
        # heating at 60 (external/uncommanded — e.g. reboot drift, physical button,
        # or a stuck device that ignores our OFF). The worker MUST POST target 0,
        # reconciling against the device's reported state, not just its own last
        # write. Previously this sent nothing (0 == last_sent 0) and the heater
        # could stay on until the device's 5-minute watchdog.
        self.dev.status_body["target"] = 60.0
        self.dev.status_body["heating"] = True
        self.dev.reflect_target = False   # device stays "hot" despite our OFF
        t = self._transport()             # desired target defaults to 0
        t.start()
        self.assertTrue(
            _wait_until(lambda: "/target?t=0" in self.dev.paths("POST"), timeout=6.0),
            "worker never asserted OFF against externally-reported heating")

    def test_force_off_posts_even_when_already_believed_off(self):
        # force_off() must unconditionally result in an OFF POST, even if the
        # transport already thinks the target is 0.
        t = self._transport()
        t.start()
        # Let it settle at 0 (initial reconcile posts once).
        self.assertTrue(_wait_until(lambda: t._last_sent_target == 0.0, timeout=5.0))
        n_before = len([p for p in self.dev.paths("POST") if p == "/target?t=0"])
        t.force_off()
        self.assertTrue(
            _wait_until(
                lambda: len([p for p in self.dev.paths("POST") if p == "/target?t=0"])
                > n_before, timeout=5.0),
            "force_off did not produce a fresh OFF POST")

    def test_forced_off_not_delayed_by_slow_status(self):
        # A stalled GET /status must not hold up a forced OFF. The worker asserts
        # its own desired target (fast-path) BEFORE polling status, so force_off()
        # reaches /target?t=0 promptly even while /status is timing out. Long poll
        # so the worker is idle (in its wake-wait) when force_off fires.
        self.dev.status_delay = 2.0
        t = self._transport(poll=8.0)
        t.start()
        # Initial reconcile posts an OFF; then the first (slow) status runs.
        self.assertTrue(_wait_until(lambda: "/target?t=0" in self.dev.paths("POST"),
                                    timeout=3.0))
        time.sleep(2.3)  # let the first slow /status finish; worker now idle
        n_before = len([p for p in self.dev.paths("POST") if p == "/target?t=0"])
        start = time.monotonic()
        t.force_off()
        got = _wait_until(
            lambda: len([p for p in self.dev.paths("POST") if p == "/target?t=0"])
            > n_before, timeout=1.0)
        elapsed = time.monotonic() - start
        self.assertTrue(got, "forced OFF was not delivered")
        self.assertLess(elapsed, 1.0,
                        "forced OFF took %.2fs — delayed behind slow /status" % elapsed)

    def test_reset_is_enqueued_not_blocking(self):
        self.dev.target_delay = 0.0
        t = self._transport()
        t.start()
        start = time.monotonic()
        t.reset_fault()
        self.assertLess(time.monotonic() - start, 0.05,
                        "reset_fault blocked the caller")
        self.assertTrue(_wait_until(lambda: "/reset" in self.dev.paths("POST"),
                                    timeout=5.0),
                        "worker never POSTed the reset")


if __name__ == "__main__":
    unittest.main(verbosity=2)
