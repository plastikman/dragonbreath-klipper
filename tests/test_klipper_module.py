"""DragonBreath Klipper-side tests against fake Klipper objects (stdlib only)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dragonbreath  # noqa: E402


class CommandError(Exception):
    pass


class FakeReactor:
    NOW = 0.
    NEVER = 9999999999.

    def __init__(self):
        self.now = 100.
        self.on_pause = None
        self.pauses = 0

    def monotonic(self):
        return self.now

    def pause(self, waketime):
        self.pauses += 1
        if self.pauses > 500:
            raise AssertionError("reactor wait never ended")
        self.now = max(self.now, waketime)
        if self.on_pause:
            self.on_pause()
        return self.now

    def register_timer(self, callback, waketime):
        return callback

    def update_timer(self, timer, waketime):
        pass


class FakeHeater:
    def __init__(self, printer):
        self.printer = printer
        self.target_temp = 0.
        self.temperature = 24.
        self.min_temp = 0.
        self.max_temp = 75.

    def set_temp(self, degrees):
        if degrees and (degrees < self.min_temp or degrees > self.max_temp):
            raise CommandError("out of range")
        self.target_temp = degrees

    def check_busy(self, eventtime):
        return self.temperature < self.target_temp - 2.


class FakeHeaters:
    def __init__(self, printer):
        self.heater = FakeHeater(printer)
        self.heaters = {"dragonbreath": self.heater}
        self.wait_step = None
        self.calls = []

    def add_sensor_factory(self, name, factory):
        pass

    def lookup_heater(self, name):
        return self.heater

    def set_temperature(self, heater, temp, wait=False):
        self.calls.append((temp, wait))
        heater.set_temp(temp)
        if not wait:
            return
        for _ in range(50):
            if heater.target_temp <= 0. or heater.temperature >= temp:
                return
            if self.wait_step:
                self.wait_step()
        raise AssertionError("M191 wait never ended")


class FakeGcode:
    def __init__(self):
        self.commands = {}
        self.responses = []
        self.mutex_held = False

    def register_command(self, name, func, desc=None):
        self.commands[name] = func

    def respond_info(self, msg, log=True):
        self.responses.append(msg)

    def respond_raw(self, msg):
        self.responses.append(msg)


class FakePrinter:
    command_error = CommandError

    def __init__(self):
        self.reactor = FakeReactor()
        self.heaters = FakeHeaters(self)
        self.gcode = FakeGcode()
        self.handlers = {}
        self.shutdown = False
        self.objects = {
            "heaters": self.heaters,
            "gcode": self.gcode,
            "pins": self,
        }

    def get_reactor(self):
        return self.reactor

    def load_object(self, config, name):
        return self.objects[name]

    def lookup_object(self, name, default=None):
        if name not in self.objects:
            raise KeyError(name)
        return self.objects[name]

    def register_chip(self, name, chip):
        pass

    def register_event_handler(self, event, callback):
        self.handlers.setdefault(event, []).append(callback)

    def send_event(self, event, *params):
        for cb in self.handlers.get(event, []):
            cb(*params)

    def is_shutdown(self):
        return self.shutdown


class FakeConfig:
    def __init__(self, printer, options=None):
        self.printer = printer
        self.options = dict({"host": "127.0.0.1"}, **(options or {}))

    def get_printer(self):
        return self.printer

    def get_name(self):
        return "dragonbreath"

    def get(self, key, default=None):
        return self.options.get(key, default)

    def getint(self, key, default=None, **kw):
        return default

    def getfloat(self, key, default=None, **kw):
        return default

    def getboolean(self, key, default=None):
        return default


class FakeTransport:
    """Records intents; the test decides when and how each one resolves."""

    def __init__(self):
        self.seq = 0
        self.targets = []
        self.results = {}
        self.live = True

    def set_target(self, degrees):
        self.seq += 1
        self.targets.append(float(degrees))
        return self.seq

    def force_off(self):
        self.seq += 1
        return self.seq

    def intent_result(self, seq):
        return self.results.get(seq)

    def event_stream_live(self):
        return self.live

    def start(self):
        pass

    def stop(self):
        pass


class FakeGcmd:
    def __init__(self, s):
        self.s = s

    def get_float(self, name, default=None):
        return self.s

    def error(self, msg):
        return CommandError(msg)

    def respond_info(self, msg):
        pass


GOOD_FRAME = {
    "temperature": 30.0, "target": 0.0, "requested_target": 0.0,
    "mode": "off", "source": "klipper", "state_revision": 9,
    "lease_owned": False, "countermanded": False,
}


class KlipperModuleTest(unittest.TestCase):
    options = {}

    def setUp(self):
        self.printer = FakePrinter()
        self.module = dragonbreath.DragonBreath(
            FakeConfig(self.printer, self.options))
        self.transport = FakeTransport()
        self.module._transport = self.transport
        self.module._attach_heater_hook()
        self.heater = self.printer.heaters.heater

    def poll(self):
        self.module._reactor_poll(self.printer.reactor.monotonic())

    def reject(self, seq, target, reason="countermanded",
               code="revision_conflict", clears_target=True):
        self.module._enqueue({
            "protocol_error": code, "reason": reason,
            "message": "command rejected by authoritative policy",
            "seq": seq, "target": target, "clears_target": clears_target,
        })

    def status(self):
        return self.module.get_status(self.printer.reactor.monotonic())

    def test_rejection_stays_in_status_until_the_next_intent(self):
        self.heater.set_temp(60.0)
        seq = self.transport.seq
        self.reject(seq, 60.0)
        self.poll()
        self.module._enqueue(dict(GOOD_FRAME))
        self.poll()
        status = self.status()
        self.assertEqual("revision_conflict", status["protocol_error"])
        self.assertEqual("countermanded", status["protocol_error_code"])
        self.assertEqual(60.0, status["protocol_error_target"])
        self.assertEqual(1, status["protocol_error_seq"])
        self.assertEqual(0.0, self.heater.target_temp)
        self.assertEqual(0.0, status["target"])

        self.heater.set_temp(60.0)
        status = self.status()
        self.assertIsNone(status["protocol_error"])
        self.assertIsNone(status["protocol_error_code"])
        self.assertIsNone(status["protocol_error_target"])
        self.assertEqual(1, status["protocol_error_seq"])

    def test_final_rejection_warns_on_console_without_error_prefix(self):
        self.heater.set_temp(60.0)
        self.reject(self.transport.seq, 60.0)
        self.poll()
        self.assertEqual(1, len(self.printer.gcode.responses))
        line = self.printer.gcode.responses[0]
        self.assertFalse(line.startswith("!!"))
        self.assertIn("60", line)
        self.assertIn("countermanded", line)

    def test_rejection_of_an_older_intent_keeps_the_newer_target(self):
        self.heater.set_temp(60.0)
        old = self.transport.seq
        self.heater.set_temp(65.0)
        self.reject(old, 60.0)
        self.poll()
        self.assertEqual(65.0, self.heater.target_temp)
        self.assertEqual(65.0, self.module.target)
        self.assertEqual([], self.printer.gcode.responses)

    def test_fan_rejection_keeps_the_heater_target(self):
        self.heater.set_temp(60.0)
        self.reject(None, None, reason="heater_busy", code="heater_busy",
                    clears_target=False)
        self.poll()
        self.assertEqual(60.0, self.heater.target_temp)
        self.assertEqual("heater_busy", self.status()["protocol_error"])

    def test_status_reports_event_stream_liveness(self):
        self.transport.live = False
        self.assertFalse(self.status()["event_stream_live"])

    def test_m141_rejection_does_not_raise(self):
        self.printer.gcode.commands["M141"](FakeGcmd(60.0))
        self.reject(self.transport.seq, 60.0)
        self.poll()
        self.assertEqual(0.0, self.heater.target_temp)

    def deliver_rejection(self, reason="countermanded"):
        self.reject(self.transport.seq, 60.0, reason=reason)
        self.poll()

    def deliver_countermand(self):
        frame = dict(GOOD_FRAME, source="button", countermanded=True)
        self.module._enqueue(frame)
        self.poll()

    def warnings(self):
        return [r for r in self.printer.gcode.responses
                if "DragonBreath" in r]

    def test_m141_rejection_warns_once(self):
        self.printer.gcode.commands["M141"](FakeGcmd(60.0))
        self.deliver_rejection()
        self.assertEqual(1, len(self.warnings()))

    def test_fan_rejection_clears_on_the_next_state_frame(self):
        self.reject(None, None, reason="heater_busy", code="heater_busy",
                    clears_target=False)
        self.poll()
        self.assertEqual("heater_busy", self.status()["protocol_error"])
        self.module._enqueue(dict(GOOD_FRAME))
        self.poll()
        self.assertIsNone(self.status()["protocol_error"])
        self.assertIsNone(self.status()["protocol_error_code"])
        self.assertEqual(1, self.status()["protocol_error_seq"])

    def test_fan_rejection_clears_on_the_next_fan_intent(self):
        self.transport.set_fan = lambda percent: None
        self.reject(None, None, reason="heater_busy", code="heater_busy",
                    clears_target=False)
        self.poll()
        self.module.set_fan_percent(0)
        self.assertIsNone(self.status()["protocol_error"])

    def test_heat_rejection_survives_a_fan_intent(self):
        self.transport.set_fan = lambda percent: None
        self.heater.set_temp(60.0)
        self.reject(self.transport.seq, 60.0)
        self.poll()
        self.module.set_fan_percent(0)
        self.assertEqual("revision_conflict", self.status()["protocol_error"])



if __name__ == "__main__":
    unittest.main(verbosity=2)
