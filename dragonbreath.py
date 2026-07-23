# dragonbreath.py — Klipper extras module for the DragonBreath chamber heater.
#
# Surfaces an DragonBreath-firmware Panda Breath as a standard Klipper heater
# (heater_generic interface), so it shows up in Fluidd/Mainsail as a chamber
# heater with a live temperature and a settable target, and can be driven with
# M141 / M191.
#
# It talks only to DragonBreath API v2:
#   GET  /api/v2/info, /api/v2/state, /api/v2/events
#   POST /api/v2/command, /api/v2/heartbeat
# Every mutating call carries the X-DragonBreath-Auth header (CSRF gate / token).
#
# This is a focused fork of Justin Hayes' pandabreath-klipper: it keeps the
# proven Klipper glue (sensor factory + virtual pin -> heater_generic, the
# reactor-poll sensor feed, and the fail-safe force-off) and drops the stock
# firmware's WebSocket/MQTT transports and work-mode/drying/filament machinery,
# which DragonBreath does not use.
#
# No external Python dependencies — stdlib only.
#
# printer.cfg:
#   [dragonbreath]
#   host: 10.168.2.53        # DragonBreath device IP or hostname
#   #port: 80
#   #token: web              # X-DragonBreath-Auth value; set this if you configured
#                            # a control token on the device (NVS ctl_token)
#   #poll_interval: 2.0        # API v2 polling fallback / retry base interval
#
#   [heater_generic dragonbreath]
#   heater_pin: dragonbreath:pwm
#   sensor_type: dragonbreath
#   control: watermark
#   max_delta: 2.0
#   min_temp: 0
#   max_temp: 75
#
#   [verify_heater dragonbreath]
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
import uuid

logger = logging.getLogger(__name__)

# How often the reactor timer drains the state queue + syncs the target (s).
REACTOR_POLL = 1.
# Warn if no fresh temperature has arrived within this window (s).
TEMP_STALE_WARN = 30.
# Warn when a single HTTP request takes at least this long (ms). This is the
# latency that, on the reactor thread, would starve the MCU command queue and
# trip "Timer Too Close" — so it is worth flagging even now that all HTTP runs
# on the worker thread (also useful for chasing the separate Wi-Fi-flap bug).
HTTP_SLOW_WARN_MS = 1000.
# Cap the exponential write-retry backoff at poll * 2**this.
WRITE_BACKOFF_MAX_SHIFT = 3
API_VERSION = 2
HEARTBEAT_INTERVAL = 30.


# ─── DragonBreath HTTP transport ────────────────────────────────────────────────

class _DragonBreathHTTP:
    """Asynchronous DragonBreath API v2 controller and state observer.

    The Klipper reactor only records intent and drains state callbacks. A command
    worker owns all command/heartbeat/poll requests; a second daemon consumes the
    device's SSE stream. The full device snapshot is authoritative. Exactly one
    accepted POWER_ON command yields exactly one lease, and only that lease is
    heartbeated. If a button, dashboard, safety transition, or another controller
    supersedes it, the helper drops ownership and does not silently re-arm.
    """

    def __init__(self, host, port, token, on_message, on_disconnect, poll):
        self._base = "http://%s:%d" % (host, port)
        self._token = token
        self._on_message = on_message
        self._on_disconnect = on_disconnect
        self._poll = poll
        self._running = False
        self._thread = None
        self._event_thread = None
        self._event_response = None
        self._event_live = False
        self._state_lock = threading.Lock()
        self._latest_state = None
        self._lease_id = None
        self._actor_id = "klippy-" + uuid.uuid4().hex[:12]
        self._desired_target = 0.
        self._intent_seq = 0
        self._applied_seq = -1
        self._pending = None
        self._applied_target = None
        self._cmd_queue = collections.deque()
        self._wake = threading.Event()
        self._last_protocol_error = None

    # -- lifecycle --
    def start(self):
        if self._running:
            return
        self._running = True
        self._event_thread = threading.Thread(
            target=self._event_run, name="dragonbreath_events", daemon=True)
        self._thread = threading.Thread(
            target=self._run, name="dragonbreath_http", daemon=True)
        self._event_thread.start()
        self._thread.start()

    def stop(self):
        with self._state_lock:
            self._desired_target = 0.
            self._intent_seq += 1
            self._lease_id = None
        self._running = False
        self._wake.set()

    def _run(self):
        fail_shift = 0
        next_heartbeat = 0.
        while self._running:
            cycle_ok = True
            try:
                if self._latest_state is None:
                    self._handshake()
                    self._get_state()
                if not self._drain_commands():
                    cycle_ok = False
                if not self._sync_intent():
                    cycle_ok = False
                if not self._event_live:
                    self._get_state()
                now = time.monotonic()
                with self._state_lock:
                    lease = self._lease_id
                if lease and now >= next_heartbeat:
                    if self._heartbeat(lease):
                        next_heartbeat = now + HEARTBEAT_INTERVAL
                    else:
                        cycle_ok = False
            except Exception as exc:
                logger.debug("dragonbreath: worker cycle error: %s", exc)
                self._on_disconnect()
                cycle_ok = False
            fail_shift = 0 if cycle_ok else min(
                fail_shift + 1, WRITE_BACKOFF_MAX_SHIFT)
            delay = self._poll * (1 << fail_shift)
            if self._wake.wait(timeout=delay):
                self._wake.clear()
        # Best-effort v2 OFF from the worker. If delivery fails, heartbeats have
        # already stopped and the device lease expires fail-safe.
        try:
            self._send_command("off", {}, expected_revision=None,
                               request_id=uuid.uuid4().hex, quiet=True)
        except Exception:
            pass

    # -- control (called on the reactor thread; assign/enqueue only, never I/O) --
    def set_target(self, degrees):
        with self._state_lock:
            self._desired_target = max(0., float(degrees))
            if self._desired_target <= 0.:
                self._lease_id = None
            self._intent_seq += 1
            self._pending = None
        self._wake.set()

    def force_off(self):
        with self._state_lock:
            self._desired_target = 0.
            self._intent_seq += 1
            self._pending = None
            self._lease_id = None
        self._wake.set()

    def reset_fault(self):
        self._cmd_queue.append({
            "command": "clear_fault",
            "request_id": uuid.uuid4().hex,
            "expected_revision": self._current_revision(),
        })
        self._wake.set()

    # -- worker-side command path --
    def _drain_commands(self):
        ok = True
        while True:
            try:
                pending = self._cmd_queue.popleft()
            except IndexError:
                break
            if pending["expected_revision"] is None:
                try:
                    self._get_state()
                except Exception:
                    self._cmd_queue.appendleft(pending)
                    return False
                pending["expected_revision"] = self._current_revision()
            try:
                status, data = self._send_command(
                    pending["command"], {}, pending["expected_revision"],
                    pending["request_id"])
            except Exception:
                self._cmd_queue.appendleft(pending)
                return False
            if 200 <= status < 300:
                self._accept_state(data.get("state"))
            elif status in (400, 403, 409):
                self._accept_state(data.get("state"))
                self._notify_protocol_error(data)
            else:
                self._cmd_queue.appendleft(pending)
                ok = False
                break
        return ok

    def _sync_intent(self):
        with self._state_lock:
            seq = self._intent_seq
            desired = self._desired_target
            pending = self._pending
        if seq == self._applied_seq:
            return True
        if pending is None or pending["seq"] != seq:
            command = "off" if desired <= 0. else "power_on"
            fields = {} if command == "off" else {"target_c": desired}
            revision = None
            if command != "off":
                revision = self._current_revision()
                if revision is None:
                    self._get_state()
                    revision = self._current_revision()
                    if revision is None:
                        return False
            pending = {
                "seq": seq,
                "target": desired,
                "command": command,
                "fields": fields,
                "request_id": uuid.uuid4().hex,
                "expected_revision": revision,
            }
            with self._state_lock:
                self._pending = pending
        try:
            status, data = self._send_command(
                pending["command"], pending["fields"],
                pending["expected_revision"],
                pending["request_id"])
        except Exception:
            return False  # retry the same request_id: firmware replay is idempotent
        if 200 <= status < 300:
            self._accept_state(data.get("state"))
            self._applied_target = pending["target"]
            self._applied_seq = pending["seq"]
            with self._state_lock:
                if self._pending is pending:
                    self._pending = None
            return True
        if status in (400, 403, 409):
            self._accept_state(data.get("state"))
            self._notify_protocol_error(data)
            self._applied_seq = pending["seq"]  # explicit rejection is terminal
            with self._state_lock:
                if pending["target"] > 0.:
                    self._desired_target = 0.
                    self._lease_id = None
                if self._pending is pending:
                    self._pending = None
            return True
        return False

    def _send_command(self, name, fields, expected_revision, request_id,
                      quiet=False):
        body = {
            "api_version": API_VERSION,
            "request_id": request_id,
            "actor": {"kind": "klipper", "id": self._actor_id},
            "command": dict({"name": name}, **fields),
        }
        if expected_revision is not None:
            body["expected_revision"] = expected_revision
        return self._request_json("POST", "/api/v2/command", body, quiet=quiet)

    def _heartbeat(self, lease_id):
        status, data = self._request_json(
            "POST", "/api/v2/heartbeat",
            {"api_version": API_VERSION, "lease_id": lease_id}, quiet=True)
        if 200 <= status < 300:
            return True
        self._accept_state(data.get("state"))
        self._notify_protocol_error(data)
        if status in (400, 403, 409):
            with self._state_lock:
                if self._lease_id == lease_id:
                    self._lease_id = None
                    self._desired_target = 0.
            return True
        return False

    # -- authoritative state + API handshake --
    def _handshake(self):
        status, data = self._request_json("GET", "/api/v2/info", quiet=True)
        if status != 200 or data.get("api_version") != API_VERSION:
            self._notify_protocol_error({"error": "unsupported_api_version"})
            raise RuntimeError(
                "DragonBreath API v2 required (device returned HTTP %s, API %r)"
                % (status, data.get("api_version")))

    def _get_state(self):
        status, data = self._request_json("GET", "/api/v2/state", quiet=True)
        if status != 200:
            raise RuntimeError("GET /api/v2/state returned HTTP %s" % status)
        self._accept_state(data)
        return data

    def _current_revision(self):
        with self._state_lock:
            if self._latest_state is None:
                return None
            return int(self._latest_state["state_revision"])

    def _accept_state(self, data):
        if not isinstance(data, dict) or data.get("api_version") != API_VERSION:
            return False
        try:
            revision = int(data["state_revision"])
            boot_id = str(data["boot_id"])
            lease = data["control"]["lease"]
        except (KeyError, TypeError, ValueError):
            return False

        with self._state_lock:
            old = self._latest_state
            if (old is not None and old.get("boot_id") == boot_id
                    and revision < int(old.get("state_revision", 0))):
                return False
            old_lease = self._lease_id
            if old is not None and old.get("boot_id") != boot_id:
                self._lease_id = None
            active_id = lease.get("id") if lease.get("active") else None
            if (active_id and self._desired_target > 0.
                    and lease.get("owner") == self._actor_id
                    and data.get("source") == "klipper"):
                self._lease_id = active_id
            elif self._lease_id and active_id != self._lease_id:
                self._lease_id = None
                self._desired_target = 0.
            self._latest_state = data
            owned = bool(active_id and active_id == self._lease_id)
            countermanded = bool(old_lease and not owned)

        flat = self._flatten_state(data)
        flat["lease_owned"] = owned
        flat["countermanded"] = countermanded
        self._on_message(flat)
        return True

    def _flatten_state(self, data):
        sensors = data.get("sensors") or {}
        chamber = sensors.get("chamber") or {}
        ptc = sensors.get("ptc") or {}
        target = data.get("target") or {}
        heater = data.get("heater") or {}
        fan = data.get("fan") or {}
        environment = data.get("environment") or {}
        safety = data.get("safety") or {}
        control = data.get("control") or {}
        lease = control.get("lease") or {}
        return {
            "temperature": chamber.get("temperature_c"),
            "chamber_status": chamber.get("status"),
            "target": target.get("effective_c"),
            "requested_target": target.get("requested_c"),
            "ptc": ptc.get("temperature_c"),
            "ptc_status": ptc.get("status"),
            "heating": bool(heater.get("output")),
            "heater_demand": bool(heater.get("demand")),
            "fan_percent": fan.get("effective_percent"),
            "fan_reason": fan.get("reason"),
            "moonraker_connected": bool(
                environment.get("moonraker_connected")),
            "fault": bool(safety.get("fault_latched")
                          or safety.get("inhibited")),
            "inhibited": bool(safety.get("inhibited")),
            "fault_reason": safety.get("reason"),
            "mode": data.get("mode"),
            "source": data.get("source"),
            "state_revision": data.get("state_revision"),
            "boot_id": data.get("boot_id"),
            "firmware": data.get("firmware"),
            "lease_owner": lease.get("owner"),
        }

    def _notify_protocol_error(self, data):
        if not isinstance(data, dict):
            return
        code = data.get("error")
        if code:
            if code != self._last_protocol_error:
                logger.warning("dragonbreath: API command rejected: %s (%s)",
                               code, data.get("message", "no detail"))
            self._last_protocol_error = code
            self._on_message({"protocol_error": code})

    # -- SSE observer with polling fallback in the command worker --
    def _event_run(self):
        while self._running:
            try:
                req = urllib.request.Request(
                    self._base + "/api/v2/events", method="GET",
                    headers={"Accept": "text/event-stream"})
                with urllib.request.urlopen(req, timeout=6.) as response:
                    self._event_response = response
                    data_lines = []
                    for raw in response:
                        if not self._running:
                            break
                        line = raw.decode("utf-8", "replace").rstrip("\r\n")
                        if not line:
                            if data_lines:
                                state = json.loads("\n".join(data_lines))
                                if self._accept_state(state):
                                    self._event_live = True
                                data_lines = []
                            continue
                        if line.startswith("data:"):
                            data_lines.append(line[5:].lstrip())
            except urllib.error.HTTPError as exc:
                exc.close()
                logger.debug("dragonbreath: SSE HTTP %s", exc.code)
            except Exception as exc:
                logger.debug("dragonbreath: SSE disconnected: %s", exc)
            finally:
                self._event_response = None
                self._event_live = False
            if self._running:
                self._wake.set()  # command worker begins polling immediately
                time.sleep(min(1., self._poll))

    # -- HTTP helpers (stdlib urllib; never called by the reactor) --
    def _request_json(self, method, path, body=None, quiet=False):
        raw_body = None if body is None else json.dumps(
            body, separators=(",", ":")).encode("utf-8")
        headers = {"Accept": "application/json"}
        if body is not None:
            headers.update({
                "Content-Type": "application/json",
                "X-DragonBreath-Auth": self._token,
            })
        req = urllib.request.Request(
            self._base + path, data=raw_body, method=method, headers=headers)
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=4.) as r:
                raw = r.read()
                status = r.status
            self._log_latency(method, path, (time.monotonic() - t0) * 1000.,
                              True, quiet=quiet)
            return status, json.loads(raw.decode("utf-8", "replace") or "{}")
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            status = exc.code
            exc.close()
            dt_ms = (time.monotonic() - t0) * 1000.
            logger.warning("dragonbreath: %s %s -> HTTP %s in %.0fms",
                           method, path, status, dt_ms)
            try:
                data = json.loads(raw.decode("utf-8", "replace") or "{}")
            except ValueError:
                data = {}
            return status, data

    def _log_latency(self, method, path, dt_ms, ok, quiet=False):
        # A slow request is always worth a WARN — it is exactly the latency that
        # would have starved the MCU queue back when this ran on the reactor.
        if dt_ms >= HTTP_SLOW_WARN_MS:
            logger.warning("dragonbreath: %s %s -> %s in %.0fms (slow)",
                           method, path, "ok" if ok else "fail", dt_ms)
        elif quiet:
            logger.debug("dragonbreath: %s %s -> %s in %.0fms",
                         method, path, "ok" if ok else "fail", dt_ms)
        else:
            logger.info("dragonbreath: %s %s -> %s in %.0fms",
                        method, path, "ok" if ok else "fail", dt_ms)


# ─── Klipper heater module ────────────────────────────────────────────────────

class DragonBreath:
    """Klipper extras module — exposes the DragonBreath chamber as a virtual heater.

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
        self.target = 0.           # target Klipper wants (desired)
        self.device_target = 0.    # target the device reports (confirmed)
        self.device_requested_target = 0.
        self.smoothed_temp = 0.
        self.is_connected = False
        self.device_heating = False
        self.heater_demand = False
        self.device_fault = False
        self.device_inhibited = False
        self.fault_reason = None
        self.ptc_temp = 0.
        self.chamber_status = "uninit"
        self.ptc_status = "uninit"
        self.fan_percent = 0
        self.fan_reason = "off"
        self.device_moonraker_connected = False
        self.mode = "off"
        self.source = "boot"
        self.state_revision = 0
        self.boot_id = None
        self.firmware_version = None
        self.lease_owner = None
        self.lease_owned = False
        self.protocol_error = None
        self._last_temp_time = 0.
        self._in_shutdown = False
        self._external_off_lockout = False

        self._sensor = None
        self._virtual_pin = None
        self._heater = None
        self._heater_set_temp_orig = None
        self._state_queue = collections.deque()

        self._transport = _DragonBreathHTTP(
            host, port, token, self._enqueue, self._on_disconnect, poll)

        # 1. Sensor factory  -> sensor_type: dragonbreath
        pheaters = self.printer.load_object(config, 'heaters')
        pheaters.add_sensor_factory("dragonbreath", self._create_sensor)
        # 2. Virtual chip     -> heater_pin: dragonbreath:pwm
        ppins = self.printer.lookup_object('pins')
        ppins.register_chip('dragonbreath', self)

        self.printer.register_event_handler("klippy:connect", self._handle_connect)
        self.printer.register_event_handler("klippy:disconnect", self._handle_disconnect)
        self.printer.register_event_handler("klippy:shutdown", self._handle_shutdown)

        self._poll_timer = self.reactor.register_timer(
            self._reactor_poll, self.reactor.NEVER)

        gcode = self.printer.lookup_object('gcode')
        gcode.register_command("DRAGONBREATH_RESET", self._cmd_reset,
                               desc="Clear a latched DragonBreath device fault")
        if self._register_macros:
            # M141/M191 aren't native Klipper — register them for the chamber
            # heater. Opt out with register_macros: False if you define your own.
            gcode.register_command("M141", self._cmd_M141,
                                   desc="Set chamber temperature (DragonBreath)")
            gcode.register_command("M191", self._cmd_M191,
                                   desc="Set chamber temperature and wait (DragonBreath)")

    def _create_sensor(self, config):
        self._sensor = DragonBreathSensor(config, self)
        return self._sensor

    def setup_pin(self, pin_type, pin_params):
        if pin_params['pin'] == 'pwm':
            self._virtual_pin = DragonBreathVirtualPin(self)
            return self._virtual_pin
        raise self.printer.config.error(
            "Unknown dragonbreath pin: %s" % (pin_params['pin'],))

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
            logger.warning("dragonbreath: failed to force off on %s: %s", reason, exc)

    def _attach_heater_hook(self):
        if self._heater_set_temp_orig is not None:
            return
        try:
            pheaters = self.printer.lookup_object('heaters')
            heater = pheaters.lookup_heater(self.name)
        except Exception as exc:
            logger.warning("dragonbreath: unable to hook heater '%s': %s", self.name, exc)
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
            logger.debug("dragonbreath: unable to clear heater target state: %s", exc)

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
                raise gcmd.error("dragonbreath: heater '%s' not found — is "
                                 "[heater_generic %s] configured?" % (self.name, self.name))
        pheaters.set_temperature(heater, temp, wait)

    def _cmd_reset(self, gcmd):
        try:
            self._transport.reset_fault()
            gcmd.respond_info("DragonBreath: queued fault clear")
        except Exception as exc:
            raise gcmd.error("DragonBreath reset failed: %s" % exc)

    # ── state queue + reactor poll ────────────────────────────────────────────

    def _enqueue(self, data):
        self._state_queue.append(data)

    def _on_disconnect(self):
        self._state_queue.append({"_disconnected": True})

    def _reactor_poll(self, eventtime):
        while self._state_queue:
            data = self._state_queue.popleft()
            if data.get("_disconnected"):
                self.is_connected = False
                continue
            if data.get("protocol_error"):
                self.protocol_error = data["protocol_error"]
                if self.target > 0.:
                    self._clear_heater_target_state()
                    self.target = 0.
                    self._external_off_lockout = True
                continue
            self.is_connected = True
            self.protocol_error = None
            temp = data.get("temperature")
            if temp is not None:
                self.temperature = float(temp)
                self.smoothed_temp = self.temperature
                self._last_temp_time = eventtime
            if data.get("ptc") is not None:
                self.ptc_temp = float(data["ptc"])
            if data.get("target") is not None:
                self.device_target = float(data["target"])
            if data.get("requested_target") is not None:
                self.device_requested_target = float(
                    data["requested_target"])
            self.device_heating = bool(data.get("heating"))
            self.heater_demand = bool(data.get("heater_demand"))
            self.device_fault = bool(data.get("fault"))
            self.device_inhibited = bool(data.get("inhibited"))
            self.fault_reason = data.get("fault_reason")
            self.chamber_status = data.get("chamber_status") or "uninit"
            self.ptc_status = data.get("ptc_status") or "uninit"
            self.fan_percent = int(data.get("fan_percent") or 0)
            self.fan_reason = data.get("fan_reason") or "off"
            self.device_moonraker_connected = bool(
                data.get("moonraker_connected"))
            self.mode = data.get("mode") or "off"
            self.source = data.get("source") or "unknown"
            self.state_revision = int(data.get("state_revision") or 0)
            self.boot_id = data.get("boot_id")
            self.firmware_version = data.get("firmware")
            self.lease_owner = data.get("lease_owner")
            self.lease_owned = bool(data.get("lease_owned"))
            if data.get("countermanded"):
                logger.warning(
                    "dragonbreath: Klipper lease countermanded by %s; "
                    "accepting authoritative device state", self.source)
                self._clear_heater_target_state()
                self.target = 0.
                self._external_off_lockout = True

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
            logger.warning("dragonbreath: temperature data stale (%.0fs)",
                           eventtime - self._last_temp_time)
            self._last_temp_time = eventtime

        # A fresh Klipper command creates a fresh lease. Device state is otherwise
        # authoritative: physical/WebUI/safety actions are reflected, not fought.
        heater_target = self._lookup_heater_target()
        if heater_target is not None and abs(heater_target - self.target) > 0.01:
            if self._external_off_lockout and heater_target > 0.:
                logger.info("dragonbreath: ignoring synced target %.1f after forced off",
                            heater_target)
            else:
                self.set_device_target(heater_target)

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
            logger.info("dragonbreath: ignoring target %.1f while Klipper is shutdown",
                        float(degrees))
            return
        self.target = float(degrees)
        self._transport.set_target(degrees)

    def get_status(self, eventtime):
        return {
            "temperature": self.temperature,
            "target": self.target,
            "device_target": self.device_target,
            "device_requested_target": self.device_requested_target,
            "smoothed_temp": self.smoothed_temp,
            "connected": self.is_connected,
            "heating": self.device_heating,
            "heater_demand": self.heater_demand,
            "fault": self.device_fault,
            "inhibited": self.device_inhibited,
            "fault_reason": self.fault_reason,
            "ptc_temp": self.ptc_temp,
            "chamber_status": self.chamber_status,
            "ptc_status": self.ptc_status,
            "fan_percent": self.fan_percent,
            "fan_reason": self.fan_reason,
            "device_moonraker_connected": self.device_moonraker_connected,
            "api_version": API_VERSION,
            "mode": self.mode,
            "source": self.source,
            "state_revision": self.state_revision,
            "boot_id": self.boot_id,
            "firmware_version": self.firmware_version,
            "lease_owner": self.lease_owner,
            "lease_owned": self.lease_owned,
            "protocol_error": self.protocol_error,
        }


class DragonBreathSensor:
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


class DragonBreathVirtualPin:
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
            # The heater's PWM output may cycle many times at one target. A target
            # command is an ownership transition, not a PWM edge: never create a
            # fresh API lease just because watermark control toggled back on.
            elif target != self.module.target:
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
    return DragonBreath(config)
