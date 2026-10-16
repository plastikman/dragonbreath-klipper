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

    def get_temp(self, eventtime):
        return self.temperature, self.target_temp


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
        # Kalico's HEATER_INTERRUPT counter (klippy/gcode.py); None models
        # mainline and Snapmaker's Klipper, which have no such counter.
        self.interrupts = None

    def __getattr__(self, name):
        if name == "get_interrupt_counter" and self.interrupts is not None:
            return lambda: self.interrupts
        raise AttributeError(name)

    def register_command(self, name, func, desc=None):
        self.commands[name] = func

    def respond_info(self, msg, log=True):
        self.responses.append(msg)

    def respond_raw(self, msg):
        self.responses.append(msg)


class FakeWebhooks:
    def __init__(self):
        self.endpoints = {}

    def register_endpoint(self, path, callback):
        self.endpoints[path] = callback


class FakePrinter:
    command_error = CommandError

    def __init__(self):
        self.reactor = FakeReactor()
        self.heaters = FakeHeaters(self)
        self.gcode = FakeGcode()
        self.webhooks = FakeWebhooks()
        self.handlers = {}
        self.shutdown = False
        self.objects = {
            "heaters": self.heaters,
            "gcode": self.gcode,
            "webhooks": self.webhooks,
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

    def getchoice(self, key, choices, default=None):
        value = self.options.get(key, default)
        if value not in choices:
            raise ValueError("invalid choice %r" % (value,))
        return choices[value]

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


class FakeWebRequest:
    def __init__(self, params):
        self.params = params
        self.response = None

    def get_float(self, item, default=None):
        if item not in self.params:
            if default is None:
                raise CommandError("missing %s" % item)
            return default
        return float(self.params[item])

    def send(self, data):
        self.response = data


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

    # -- dragonbreath/set_target webhook --

    def call_set_target(self, params, resolve=None):
        handler = self.printer.webhooks.endpoints["dragonbreath/set_target"]
        if resolve is not None:
            pauses = []

            def on_pause():
                pauses.append(1)
                if len(pauses) == 2:
                    self.transport.results[self.transport.seq] = resolve
            self.printer.reactor.on_pause = on_pause
        # Webhooks run on the reactor while a gcode TEMPERATURE_WAIT can hold
        # the gcode mutex; the endpoint must complete regardless.
        self.printer.gcode.mutex_held = True
        request = FakeWebRequest(params)
        handler(request)
        return request.response

    def test_set_target_endpoint_applies_through_the_heater_hook(self):
        response = self.call_set_target(
            {"target": 60.0}, resolve={"accepted": True, "reason": None})
        self.assertEqual([(60.0, False)], self.printer.heaters.calls)
        self.assertEqual(60.0, self.heater.target_temp)
        self.assertEqual([60.0], self.transport.targets)
        self.assertEqual({"accepted": True, "pending": False, "reason": None,
                          "seq": self.transport.seq, "target": 60.0},
                         response)

    def test_set_target_endpoint_errors_when_the_hook_is_missing(self):
        printer = FakePrinter()
        module = dragonbreath.DragonBreath(FakeConfig(printer))
        module._transport = self.transport
        self.transport.results[0] = {"accepted": True, "reason": None}
        handler = printer.webhooks.endpoints["dragonbreath/set_target"]
        with self.assertRaises(CommandError):
            handler(FakeWebRequest({"target": 60.0}))
        self.assertEqual([], self.transport.targets)

    def test_set_target_endpoint_clears_forced_off_lockout(self):
        self.module._external_off_lockout = True
        self.call_set_target(
            {"target": 60.0}, resolve={"accepted": True, "reason": None})
        self.assertFalse(self.module._external_off_lockout)

    def test_set_target_endpoint_reports_a_rejection(self):
        response = self.call_set_target(
            {"target": 60.0},
            resolve={"accepted": False, "reason": "countermanded"})
        self.assertFalse(response["accepted"])
        self.assertFalse(response["pending"])
        self.assertEqual("countermanded", response["reason"])

    def test_set_target_endpoint_times_out_as_pending(self):
        start = self.printer.reactor.monotonic()
        response = self.call_set_target({"target": 60.0})
        self.assertIsNone(response["accepted"])
        self.assertTrue(response["pending"])
        self.assertEqual(self.transport.seq, response["seq"])
        elapsed = self.printer.reactor.monotonic() - start
        self.assertGreaterEqual(elapsed, dragonbreath.SET_TARGET_WAIT)
        self.assertLess(elapsed, dragonbreath.SET_TARGET_WAIT + 1.)

    def test_set_target_endpoint_rejects_out_of_range(self):
        with self.assertRaises(CommandError):
            self.call_set_target({"target": 500.0})
        self.assertEqual([], self.transport.targets)

    def test_set_target_endpoint_refuses_heat_while_shutdown(self):
        self.module._in_shutdown = True
        response = self.call_set_target({"target": 60.0})
        self.assertFalse(response["accepted"])
        self.assertEqual("klipper_shutdown", response["reason"])
        self.assertEqual([], self.transport.targets)

    # -- M141 / M191 --

    def test_m141_rejection_does_not_raise(self):
        self.printer.gcode.commands["M141"](FakeGcmd(60.0))
        self.reject(self.transport.seq, 60.0)
        self.poll()
        self.assertEqual(0.0, self.heater.target_temp)

    def run_m191(self, steps, target=60.0):
        """Run M191 S<target>; steps[i] runs at the i-th reactor pause."""
        pending = list(steps)

        def on_pause():
            if pending:
                pending.pop(0)()
        self.printer.reactor.on_pause = on_pause
        self.printer.gcode.commands["M191"](FakeGcmd(target))
        return pending

    def warm(self):
        self.heater.temperature = 61.0

    def deliver_rejection(self, reason="countermanded"):
        self.reject(self.transport.seq, 60.0, reason=reason)
        self.poll()

    def deliver_countermand(self):
        frame = dict(GOOD_FRAME, source="button", countermanded=True)
        self.module._enqueue(frame)
        self.poll()

    def retry_via_endpoint(self):
        self.printer.heaters.set_temperature(self.heater, 60.0, False)

    def warnings(self):
        return [r for r in self.printer.gcode.responses
                if "DragonBreath" in r]

    def assert_waited_through_zeroing(self, zero):
        nothing = lambda: None
        left = self.run_m191([zero, nothing, nothing, nothing,
                              self.retry_via_endpoint, nothing, self.warm])
        self.assertEqual([], left)  # returned only after the chamber warmed
        self.assertEqual(60.0, self.heater.target_temp)
        self.assertEqual(1, len(self.warnings()))
        self.assertFalse(self.warnings()[0].startswith("!!"))

    def test_m191_keeps_waiting_after_a_rejection_and_never_raises(self):
        self.assert_waited_through_zeroing(self.deliver_rejection)

    def test_m191_keeps_waiting_after_a_countermand(self):
        self.assert_waited_through_zeroing(self.deliver_countermand)

    def test_m191_keeps_waiting_after_the_heartbeat_loses_the_lease(self):
        self.assert_waited_through_zeroing(
            lambda: self.deliver_rejection(reason="lease_lost"))

    def test_m191_counts_one_warning_when_both_paths_fire(self):
        def both():
            self.deliver_rejection(reason="lease_lost")
            self.deliver_countermand()
        self.assert_waited_through_zeroing(both)

    def test_m191_ends_when_the_chamber_heats_with_the_target_still_zero(self):
        # Someone turns the heater back on at the box: Klipper's target stays
        # 0, but the chamber reaching the M191 value releases the print.
        nothing = lambda: None
        left = self.run_m191([self.deliver_countermand, nothing, self.warm,
                              nothing])
        self.assertEqual([nothing], left)
        self.assertEqual(0.0, self.heater.target_temp)

    def test_m191_holds_below_its_value_with_the_target_zero(self):
        def nearly():
            self.heater.temperature = 59.0
        stop = lambda: self.printer.send_event("pause_resume:cancel")
        left = self.run_m191([self.deliver_rejection, nearly, lambda: None,
                              stop, self.warm])
        self.assertEqual([self.warm], left)

    def test_m191_warning_says_how_to_continue(self):
        nothing = lambda: None
        self.run_m191([self.deliver_countermand, nothing, self.warm])
        warning = self.warnings()[0]
        self.assertFalse(warning.startswith("!!"))
        for text in ("waiting for the chamber to reach 60", "heater is off",
                     "countermanded", "at the heater", "Try again",
                     "Cancel", "Console commands"):
            self.assertIn(text, warning)

    def test_m141_rejection_warning_does_not_mention_a_wait(self):
        self.printer.gcode.commands["M141"](FakeGcmd(60.0))
        self.deliver_rejection()
        self.assertEqual(1, len(self.warnings()))
        self.assertNotIn("waiting", self.warnings()[0])

    def test_m191_ends_when_a_new_zero_target_is_set(self):
        def turn_off():
            self.printer.heaters.set_temperature(self.heater, 0., False)
        left = self.run_m191([self.deliver_rejection, turn_off, self.warm])
        self.assertEqual([self.warm], left)

    def test_m191_ends_on_print_cancel_event(self):
        cancel = lambda: self.printer.send_event("pause_resume:cancel")
        left = self.run_m191([self.deliver_rejection, cancel, self.warm])
        self.assertEqual([self.warm], left)

    def test_m191_ends_on_kalico_interrupt(self):
        self.printer.gcode.interrupts = 0

        def interrupt():
            self.printer.gcode.interrupts += 1
        left = self.run_m191([self.deliver_rejection, interrupt, self.warm])
        self.assertEqual([self.warm], left)

    def test_m191_ends_on_shutdown(self):
        def shutdown():
            self.printer.shutdown = True
        left = self.run_m191([self.deliver_rejection, shutdown, self.warm])
        self.assertEqual([self.warm], left)

    def test_m191_succeeds_when_the_chamber_reaches_target(self):
        self.assertEqual([], self.run_m191([self.warm]))
        self.assertEqual(60.0, self.heater.target_temp)

    def test_m191_publishes_its_wait_in_status(self):
        seen = []

        def look():
            seen.append((self.status()["wait_active"],
                         self.status()["wait_target"]))
        self.assertEqual((False, None), (self.status()["wait_active"],
                                         self.status()["wait_target"]))
        self.run_m191([look, self.deliver_rejection, look, self.warm])
        self.assertEqual([(True, 60.0), (True, 60.0)], seen)
        self.assertFalse(self.status()["wait_active"])
        self.assertIsNone(self.status()["wait_target"])

    def test_m191_wait_target_follows_a_new_target(self):
        seen = []

        def retarget():
            self.printer.heaters.set_temperature(self.heater, 65.0, False)
        look = lambda: seen.append(self.status()["wait_target"])
        stop = lambda: self.printer.send_event("pause_resume:cancel")
        self.run_m191([retarget, look, stop])
        self.assertEqual([65.0], seen)
        self.assertFalse(self.status()["wait_active"])

    def test_m191_wait_status_clears_on_shutdown(self):
        def shutdown():
            self.printer.shutdown = True
        self.run_m191([self.deliver_rejection, shutdown])
        self.assertFalse(self.status()["wait_active"])

    def test_m191_prints_the_stock_temperature_report_each_second(self):
        self.printer.heaters._get_temp = lambda eventtime: \
            "B:60.0 /60.0 dragonbreath:%.1f /%.1f" % (
                self.heater.temperature, self.heater.target_temp)
        self.run_m191([lambda: None, self.warm])
        reports = [r for r in self.printer.gcode.responses
                   if r.startswith("B:")]
        self.assertEqual(2, len(reports))

    def test_m191_zero_does_not_wait(self):
        self.printer.gcode.commands["M191"](FakeGcmd(0.))
        self.assertEqual(0, self.printer.reactor.pauses)

    # -- non-heat rejections --

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



class ContinueOnRejectionTest(KlipperModuleTest):
    """m191_on_rejection: continue keeps Klipper's own wait semantics."""
    options = {"m191_on_rejection": "continue"}

    def test_m191_returns_when_a_rejection_zeroes_its_target(self):
        left = self.run_m191([self.deliver_rejection, self.warm])
        self.assertEqual([self.warm], left)
        self.assertFalse(self.status()["wait_active"])

    def test_m191_returns_when_a_countermand_zeroes_its_target(self):
        left = self.run_m191([self.deliver_countermand, self.warm])
        self.assertEqual([self.warm], left)

    def test_warning_does_not_claim_the_print_is_waiting(self):
        self.run_m191([self.deliver_rejection, self.warm])
        self.assertEqual(1, len(self.warnings()))
        self.assertNotIn("waiting", self.warnings()[0])


# Only the tests above run in continue mode; the inherited ones pin wait mode.
for _name in dir(KlipperModuleTest):
    if _name.startswith("test") and _name not in vars(ContinueOnRejectionTest):
        setattr(ContinueOnRejectionTest, _name, None)


class ConfigTest(unittest.TestCase):
    def test_m191_on_rejection_rejects_unknown_values(self):
        printer = FakePrinter()
        with self.assertRaises(ValueError):
            dragonbreath.DragonBreath(
                FakeConfig(printer, {"m191_on_rejection": "pause"}))

    def test_m191_on_rejection_defaults_to_wait(self):
        printer = FakePrinter()
        module = dragonbreath.DragonBreath(FakeConfig(printer))
        self.assertEqual("wait", module._m191_on_rejection)


if __name__ == "__main__":
    unittest.main(verbosity=2)
