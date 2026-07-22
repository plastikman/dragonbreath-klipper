# openbreath.py — Klipper extras module for the OpenBreath chamber heater.
#
# Surfaces an OpenBreath-firmware Panda Breath as a standard Klipper heater
# (heater_generic interface), so it shows up in Fluidd/Mainsail as a chamber
# heater with a live temperature and a settable target, and can be driven with
# M141 / M191.
#
# It talks to the OpenBreath HTTP control API (see the OpenBreath firmware):
#   GET  /status        -> {temp,target,heating,fault,fault_reason,...}
#   POST /target?t=<C>  -> set chamber setpoint (0 = off); counts as liveness
#   POST /heartbeat     -> controller liveness (feeds the device comms watchdog)
#   POST /reset         -> clear a latched device fault
# Every mutating call carries the X-OpenBreath-Auth header (CSRF gate / token).
#
# This is a focused fork of Justin Hayes' pandabreath-klipper: it keeps the
# proven Klipper glue (sensor factory + virtual pin -> heater_generic, the
# reactor-poll sensor feed, and the fail-safe force-off) and drops the stock
# firmware's WebSocket/MQTT transports and work-mode/drying/filament machinery,
# which OpenBreath does not use.
#
# No external Python dependencies — stdlib only.
#
# printer.cfg:
#   [openbreath]
#   host: 10.168.2.53        # OpenBreath device IP or hostname
#   #port: 80
#   #token: web              # X-OpenBreath-Auth value; set this if you configured
#                            # a control token on the device (NVS ctl_token)
#   #poll_interval: 2.0
#
#   [heater_generic openbreath]
#   heater_pin: openbreath:pwm
#   sensor_type: openbreath
#   control: watermark
#   max_delta: 2.0
#   min_temp: 0
#   max_temp: 75
#
#   [verify_heater openbreath]
#   check_gain_time: 300
#   hysteresis: 5
#   heating_gain: 1
#
# M141/M191 are registered automatically (set register_macros: False to opt out).

import json
import logging
import threading
import time
import urllib.request
import urllib.error
import collections

logger = logging.getLogger(__name__)

# How often the reactor timer drains the state queue + syncs the target (s).
REACTOR_POLL = 1.
# Warn if no fresh temperature has arrived within this window (s).
TEMP_STALE_WARN = 30.


# ─── OpenBreath HTTP transport ────────────────────────────────────────────────

class _OpenBreathHTTP:
    """Background HTTP client for the OpenBreath control API.

    A daemon thread polls GET /status and hands each reading to on_message().
    While a positive target is commanded it also POSTs /heartbeat every poll, so
    the device's comms watchdog stays fed only as long as Klipper is alive — if
    Klippy crashes/hangs, heartbeats stop and the device latches the heater off.
    Control writes (set_target) carry the X-OpenBreath-Auth header.
    """

    def __init__(self, host, port, token, on_message, on_disconnect, poll):
        self._base = "http://%s:%d" % (host, port)
        self._token = token
        self._on_message = on_message
        self._on_disconnect = on_disconnect
        self._poll = poll
        self._running = False
        self._thread = None
        self._last_target = 0.
        self._io_lock = threading.Lock()

    # -- lifecycle --
    def start(self):
        self._running = True
        self._thread = threading.Thread(
            target=self._run, name="openbreath_http", daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def _run(self):
        while self._running:
            try:
                st = self._get_status()
                if st is not None:
                    self._on_message(st)
                    if self._last_target > 0.:
                        self._post("/heartbeat")   # keep the device watchdog fed
                else:
                    self._on_disconnect()
            except Exception as exc:
                logger.debug("openbreath: poll error: %s", exc)
                self._on_disconnect()
            time.sleep(self._poll)

    # -- control --
    def set_target(self, degrees):
        degrees = max(0., float(degrees))
        self._last_target = degrees
        # A target command also counts as liveness on the device side.
        self._post("/target?t=%g" % degrees)

    def force_off(self):
        self.set_target(0.)

    def reset_fault(self):
        self._post("/reset")

    # -- HTTP helpers (stdlib urllib) --
    def _get_status(self):
        req = urllib.request.Request(self._base + "/status", method="GET")
        with urllib.request.urlopen(req, timeout=4.) as r:
            raw = r.read()
        data = json.loads(raw.decode("utf-8", "replace"))
        state = {}
        # OpenBreath emits JSON null for a non-OK sensor; treat that as "no reading"
        # rather than 0 so a sensor fault can't masquerade as a cold chamber.
        if data.get("temp") is not None:
            state["temperature"] = float(data["temp"])
        if data.get("target") is not None:
            state["target"] = float(data["target"])
        if data.get("ptc") is not None:
            state["ptc"] = float(data["ptc"])
        state["heating"] = bool(data.get("heating"))
        state["fault"] = bool(data.get("fault"))
        state["fault_reason"] = data.get("fault_reason")
        return state

    def _post(self, path):
        req = urllib.request.Request(
            self._base + path, data=b"", method="POST",
            headers={"X-OpenBreath-Auth": self._token})
        with self._io_lock:
            try:
                with urllib.request.urlopen(req, timeout=4.) as r:
                    r.read()
            except urllib.error.HTTPError as exc:
                # 409 (fault latched / inhibited) and 403 (bad token) are actionable
                # — surface them; other errors bubble to the poll loop's handler.
                logger.warning("openbreath: POST %s -> HTTP %s", path, exc.code)


# ─── Klipper heater module ────────────────────────────────────────────────────

class OpenBreath:
    """Klipper extras module — exposes the OpenBreath chamber as a virtual heater.

    Registers a sensor factory and a virtual PWM chip so the user can declare a
    standard [heater_generic] against it; the heater's set_temp is hooked so a
    commanded target is pushed to the device over HTTP, and a reactor timer feeds
    the device's temperature back into the heater's sensor callback.
    """

    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.name = config.get_name().split()[-1]

        host = config.get("host")
        port = config.getint("port", 80)
        token = config.get("token", "web")
        poll = config.getfloat("poll_interval", 2.0, above=0.25, maxval=30.)
        self._register_macros = config.getboolean("register_macros", True)

        # State (owned by the reactor thread).
        self.temperature = 0.
        self.target = 0.
        self.smoothed_temp = 0.
        self.is_connected = False
        self.device_heating = False
        self.device_fault = False
        self.fault_reason = None
        self.ptc_temp = 0.
        self._last_temp_time = 0.
        self._in_shutdown = False
        self._external_off_lockout = False

        self._sensor = None
        self._virtual_pin = None
        self._heater = None
        self._heater_set_temp_orig = None
        self._state_queue = collections.deque()

        self._transport = _OpenBreathHTTP(
            host, port, token, self._enqueue, self._on_disconnect, poll)

        # 1. Sensor factory  -> sensor_type: openbreath
        pheaters = self.printer.load_object(config, 'heaters')
        pheaters.add_sensor_factory("openbreath", self._create_sensor)
        # 2. Virtual chip     -> heater_pin: openbreath:pwm
        ppins = self.printer.lookup_object('pins')
        ppins.register_chip('openbreath', self)

        self.printer.register_event_handler("klippy:connect", self._handle_connect)
        self.printer.register_event_handler("klippy:disconnect", self._handle_disconnect)
        self.printer.register_event_handler("klippy:shutdown", self._handle_shutdown)

        self._poll_timer = self.reactor.register_timer(
            self._reactor_poll, self.reactor.NEVER)

        gcode = self.printer.lookup_object('gcode')
        gcode.register_command("OPENBREATH_RESET", self._cmd_reset,
                               desc="Clear a latched OpenBreath device fault")
        if self._register_macros:
            # M141/M191 aren't native Klipper — register them for the chamber
            # heater. Opt out with register_macros: False if you define your own.
            gcode.register_command("M141", self._cmd_M141,
                                   desc="Set chamber temperature (OpenBreath)")
            gcode.register_command("M191", self._cmd_M191,
                                   desc="Set chamber temperature and wait (OpenBreath)")

    def _create_sensor(self, config):
        self._sensor = OpenBreathSensor(config, self)
        return self._sensor

    def setup_pin(self, pin_type, pin_params):
        if pin_params['pin'] == 'pwm':
            self._virtual_pin = OpenBreathVirtualPin(self)
            return self._virtual_pin
        raise self.printer.config.error(
            "Unknown openbreath pin: %s" % (pin_params['pin'],))

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def _handle_connect(self):
        self._in_shutdown = False
        self._attach_heater_hook()
        self._transport.start()
        self.reactor.update_timer(self._poll_timer, self.reactor.NOW)
        self._force_device_off("connect")

    def _handle_disconnect(self):
        self._in_shutdown = True
        self._force_device_off("disconnect")
        self._transport.stop()
        self.reactor.update_timer(self._poll_timer, self.reactor.NEVER)

    def _handle_shutdown(self):
        # Klipper crashed/estop: force the external heater off.
        self._in_shutdown = True
        self._force_device_off("shutdown")

    def _force_device_off(self, reason):
        self._clear_heater_target_state()
        self.target = 0.
        self._external_off_lockout = True
        try:
            self._transport.force_off()
        except Exception as exc:
            logger.warning("openbreath: failed to force off on %s: %s", reason, exc)

    def _attach_heater_hook(self):
        if self._heater_set_temp_orig is not None:
            return
        try:
            pheaters = self.printer.lookup_object('heaters')
            heater = pheaters.lookup_heater(self.name)
        except Exception as exc:
            logger.warning("openbreath: unable to hook heater '%s': %s", self.name, exc)
            return
        self._heater = heater
        self._heater_set_temp_orig = heater.set_temp

        def wrapped_set_temp(degrees):
            self._heater_set_temp_orig(degrees)
            if degrees > 0.:
                self._external_off_lockout = False
            self.set_device_target(degrees)

        heater.set_temp = wrapped_set_temp

    def _clear_heater_target_state(self):
        self._attach_heater_hook()
        if self._heater_set_temp_orig is None:
            return
        try:
            self._heater_set_temp_orig(0.)
        except Exception as exc:
            logger.debug("openbreath: unable to clear heater target state: %s", exc)

    # ── gcode ───────────────────────────────────────────────────────────────

    def _cmd_M141(self, gcmd):
        self._set_heater(gcmd.get_float('S', 0.), wait=False, gcmd=gcmd)

    def _cmd_M191(self, gcmd):
        self._set_heater(gcmd.get_float('S', 0.), wait=True, gcmd=gcmd)

    def _set_heater(self, temp, wait, gcmd):
        pheaters = self.printer.lookup_object('heaters')
        heater = self._heater
        if heater is None:
            try:
                heater = pheaters.lookup_heater(self.name)
            except Exception:
                raise gcmd.error("openbreath: heater '%s' not found — is "
                                 "[heater_generic %s] configured?" % (self.name, self.name))
        pheaters.set_temperature(heater, temp, wait)

    def _cmd_reset(self, gcmd):
        try:
            self._transport.reset_fault()
            gcmd.respond_info("OpenBreath: sent fault reset")
        except Exception as exc:
            raise gcmd.error("OpenBreath reset failed: %s" % exc)

    # ── state queue + reactor poll ────────────────────────────────────────────

    def _enqueue(self, data):
        self._state_queue.append(data)

    def _on_disconnect(self):
        self.is_connected = False

    def _reactor_poll(self, eventtime):
        while self._state_queue:
            data = self._state_queue.popleft()
            self.is_connected = True
            temp = data.get("temperature")
            if temp is not None:
                self.temperature = float(temp)
                self.smoothed_temp = self.temperature
                self._last_temp_time = eventtime
            if data.get("ptc") is not None:
                self.ptc_temp = float(data["ptc"])
            self.device_heating = bool(data.get("heating"))
            self.device_fault = bool(data.get("fault"))
            self.fault_reason = data.get("fault_reason")

        # Feed the heater sensor callback every cycle, on the MCU clock so
        # verify_heater compares timestamps from the right domain.
        if self._sensor and self._sensor.callback and self._last_temp_time > 0:
            try:
                mcu = self.printer.lookup_object('mcu')
                read_time = mcu.estimated_print_time(eventtime)
            except Exception:
                read_time = eventtime
            self._sensor.callback(read_time, self.temperature)

        if (self._last_temp_time > 0.
                and eventtime - self._last_temp_time > TEMP_STALE_WARN):
            logger.warning("openbreath: temperature data stale (%.0fs)",
                           eventtime - self._last_temp_time)
            self._last_temp_time = eventtime

        # Keep the device target synced to the heater's target, and force off any
        # uncommanded device heating — the device may only heat while Klipper is
        # commanding it (belt-and-suspenders alongside the device's own watchdog).
        heater_target = self._lookup_heater_target()
        if heater_target is not None and abs(heater_target - self.target) > 0.01:
            if self._external_off_lockout and heater_target > 0.:
                logger.info("openbreath: ignoring synced target %.1f after forced off",
                            heater_target)
            else:
                self.set_device_target(heater_target)

        klipper_wants_off = (heater_target is not None and heater_target <= 0.) \
            or (heater_target is None and self.target <= 0.)
        if self.device_heating and klipper_wants_off:
            logger.warning("openbreath: device heating while Klipper commands off "
                           "— forcing off (uncommanded heating)")
            self._force_device_off("uncommanded device-on")

        return eventtime + REACTOR_POLL

    def _lookup_heater_target(self):
        try:
            pheaters = self.printer.lookup_object('heaters')
            if self._heater is None:
                self._heater = pheaters.lookup_heater(self.name)
            if self._heater is not None:
                return float(getattr(self._heater, 'target_temp', 0.0))
        except Exception:
            pass
        return None

    def set_device_target(self, degrees):
        if self._in_shutdown and float(degrees) > 0.:
            logger.info("openbreath: ignoring target %.1f while Klipper is shutdown",
                        float(degrees))
            return
        self.target = float(degrees)
        self._transport.set_target(degrees)

    def get_status(self, eventtime):
        return {
            "temperature": self.temperature,
            "target": self.target,
            "smoothed_temp": self.smoothed_temp,
            "connected": self.is_connected,
            "heating": self.device_heating,
            "fault": self.device_fault,
            "fault_reason": self.fault_reason,
            "ptc_temp": self.ptc_temp,
        }


class OpenBreathSensor:
    """Klipper sensor interface for heater_generic."""
    def __init__(self, config, module):
        self.printer = config.get_printer()
        self.module = module
        self.callback = None

    def get_temp(self, eventtime):
        return self.module.temperature, self.module.target

    def get_status(self, eventtime):
        return {
            "temperature": self.module.temperature,
            "target": self.module.target,
            "smoothed_temp": self.module.smoothed_temp,
        }

    def setup_minmax(self, min_temp, max_temp):
        pass

    def setup_callback(self, cb):
        self.callback = cb

    def get_report_time_delta(self):
        return 1.0

    def set_read_tolerance(self, range_check_val, range_check_time):
        pass


class OpenBreathVirtualPin:
    """Virtual PWM pin that intercepts heater power to sync the target temp."""
    def __init__(self, module):
        self.module = module
        self.last_value = 0.0

    def get_mcu(self):
        return self.module.printer.lookup_object('mcu')

    def set_pwm(self, print_time, value, cycle_time=None):
        target = self._lookup_heater_target()
        if target is not None:
            if target <= 0:
                if self.module.target != 0:
                    self.module.set_device_target(0)
            elif self.module._external_off_lockout:
                pass
            elif target != self.module.target or self.last_value == 0:
                self.module.set_device_target(target)
        self.last_value = value

    def _lookup_heater_target(self):
        try:
            pheaters = self.module.printer.lookup_object('heaters')
            for _, heater in pheaters.heaters.items():
                if getattr(heater, 'mcu_pwm', None) == self:
                    return heater.target_temp
        except Exception:
            pass
        return None

    def setup_max_duration(self, max_duration):
        pass

    def setup_cycle_time(self, cycle_time, shutdown_value=0.):
        pass

    def setup_start_value(self, start_value, shutdown_value):
        pass


def load_config(config):
    return OpenBreath(config)
