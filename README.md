# dragonbreath-klipper

Klipper `extras` module that surfaces an **[DragonBreath](https://github.com/plastikman/DragonBreath)**-firmware
BIGTREETECH Panda Breath as a standard chamber heater — live temperature + a
settable target in **Fluidd/Mainsail**, driven with **M141 / M191**.

It talks to DragonBreath's HTTP control API over your LAN (no MQTT broker, no cloud).

> Focused fork of Justin Hayes' [pandabreath-klipper](https://github.com/justinh-rahb/pandabreath-klipper).
> It keeps the proven Klipper glue (sensor factory + virtual pin → `heater_generic`,
> the reactor-poll sensor feed, and the fail-safe force-off) and drops the stock
> firmware's WebSocket/MQTT transports and work-mode/drying/filament machinery,
> which DragonBreath doesn't use. The transport is a small stdlib HTTP client.

## How it works
- Registers a `sensor_type: dragonbreath` + a `heater_pin: dragonbreath:pwm` virtual
  pin, so a normal `[heater_generic]` renders as a chamber heater card in Fluidd.
- **All network I/O runs on background threads** — Klipper's reactor never
  blocks on the device. Setting the heater target (M141 /
  `SET_HEATER_TEMPERATURE` / the Fluidd card) records one explicit intent and
  returns immediately. The command worker sends a revision-aware API v2 command;
  retries reuse the same request ID.
- The helper consumes `GET /api/v2/events` (SSE) and uses serialized
  `GET /api/v2/state` polling only as a reconnect fallback. Complete device
  snapshots feed the heater sensor callback on the MCU clock, so
  `verify_heater` still works. If the device's limited SSE slots are busy,
  reconnect attempts back off while state polling continues, and a sustained
  busy stream is logged once at INFO.
- An accepted POWER_ON returns a device-issued lease privately in that
  authenticated command response. Public state/events expose only lease
  ownership and expiry metadata. The helper heartbeats only its exact private
  lease. If a button, WebUI, safety transition, or another controller supersedes
  it, DragonBreath invalidates the lease and the helper accepts that authoritative
  state instead of silently restoring its old target.
- If Klipper crashes/hangs, lease heartbeats stop and DragonBreath's own watchdog
  latches the heater off — **that watchdog, not the OFF request, is the real
  fail-safe.** On orderly disconnect/shutdown the worker also sends an
  unconditional API v2 OFF.
- Every mutating request carries the `X-DragonBreath-Auth` header.

This helper requires DragonBreath firmware API v2. It does not probe or fall
back to the removed alpha `/status`, `/target`, `/heartbeat`, or `/reset` routes.

## Install
```bash
cd ~
git clone https://github.com/plastikman/dragonbreath-klipper
cd dragonbreath-klipper
./install.sh
```
`install.sh` symlinks `dragonbreath.py` into `~/klipper/klippy/extras/` and restarts
Klipper. Add the Moonraker update-manager block below to get updates in Fluidd.

## Configuration
```ini
[dragonbreath]
host: 10.168.2.53          # DragonBreath device IP or hostname (dragonbreath.local also works)
#port: 80
#token: web                # X-DragonBreath-Auth value. Leave as "web" unless you set
                           # a control token on the device (NVS ctl_token); then match it.
#poll_interval: 2.0        # API v2 polling fallback / retry base interval
#register_macros: True     # register M141/M191 (set False if you define your own)
#m191_on_rejection: wait   # wait | continue - M191 after a refused/overridden target

[heater_generic dragonbreath]
heater_pin: dragonbreath:pwm
sensor_type: dragonbreath
control: watermark
max_delta: 2.0
min_temp: 0
max_temp: 75               # DragonBreath firmware hard-caps the target at 70 C

[verify_heater dragonbreath]
check_gain_time: 300       # PTC chamber heaters are slow — be generous
hysteresis: 5
heating_gain: 1
```
The `[dragonbreath]` name and the `[heater_generic dragonbreath]` name must match.

## Usage
- **Fluidd/Mainsail:** the `dragonbreath` heater appears with the other heaters —
  set the target from its card.
- **G-code:**
  - `M141 S45` — set chamber to 45 °C (0 = off)
  - `M191 S45` — set chamber to 45 °C and wait until reached (use in print start)
  - `SET_HEATER_TEMPERATURE HEATER=dragonbreath TARGET=45`
  - `DRAGONBREATH_RESET` — clear a latched device fault (over-temp / sensor / comms)

Extra authoritative status is exposed on the `dragonbreath` printer object for
macros/dashboards: `heating`, `fault`, `inhibited`, `fault_reason`, `ptc_temp`,
`connected`, `mode`, `source`, `state_revision`, `firmware_version`,
`lease_owner`, `lease_owned`, `heater_demand`, `fan_percent`, `fan_reason`,
`device_moonraker_connected`, and `event_stream_live` (false while the helper
is polling because the SSE stream is unavailable).

### When the device refuses a target
The device rejects a heat command whose `expected_revision` is stale. Its own
policy also advances the revision (AUTO engage, the fan-only filtration band
following the bed setpoint), so `M140` right before `M141` can race it. When the
fresh state shows nobody countermanded the request (same mode, target and
source, no other lease holder, no fault), the helper re-sends the same target
with the fresh revision, up to twice. A watchdog timer edge that leaves the
device idle (a local run or drying cycle ending) is retried too, unless the lease
that expired was Klipper's own. A button, WebUI or safety change is final, as is
any other rejection (`fault_latched`, `inhibited`, ...). These rules rely on how
the firmware stamps `source` on each revision, verified against DragonBreath
firmware v1.1.18 (API v2); other builds should be checked against them.

A final rejection, or a button/WebUI countermand, sets the Klipper target to 0
and prints one console line. A rejected heat command stays in status until the
next target is set; a fan or handshake rejection clears on the next good state
or fan request:

| field | meaning |
|-------|---------|
| `protocol_error` | firmware error code (`revision_conflict`, `fault_latched`, ...) or `null` |
| `protocol_error_code` | stable reason: `countermanded`, `conflict_retries_exhausted`, `fault_latched`, `inhibited`, `invalid_command`, `auth_failed`, `missing_lease_id`, `lease_lost`, `heater_busy`, or another firmware code |
| `protocol_error_message` | firmware message text, or `null` |
| `protocol_error_target` | the target (C) the rejected request asked for, or `null` |
| `protocol_error_seq` | counts rejections since Klipper started; never resets |

Neither `M141` nor `M191` raises on a rejection. `M191` never treats a zeroed
target as reached: it keeps waiting until the chamber reaches its `S` value,
whatever heats it (a retry such as `dragonbreath/set_target`, or the heater
turned back on at the box). It also stops waiting when a target of 0 is set, the
print is cancelled (`pause_resume:cancel`, which Mainsail, Fluidd and HelixScreen
Cancel send, or a Kalico `HEATER_INTERRUPT`), or Klipper shuts down. There is no
timeout and no automatic pause. While it waits it holds the G-code lock, so
console commands (including `CANCEL_PRINT` and `RESTART`) queue until it ends;
the console warning says so and lists the ways out.

`m191_on_rejection` chooses this behaviour:

- `wait` (default): M191 holds the print as described above. The trade-off is
  that it holds the G-code lock, so the only ways out are the chamber reaching
  temperature, a Cancel from the printer UI (`printer.print.cancel`), an
  emergency stop, or `dragonbreath/set_target`.
- `continue`: M191 returns as soon as its target is zeroed, like Klipper's own
  heater wait, and the print carries on with the chamber unheated. A slicer
  `TEMPERATURE_WAIT` on the chamber sensor can then hang the print instead.

While `M191` waits, the `dragonbreath` object reports `wait_active: true` and
`wait_target` (the temperature it is holding for, following a newer target set
during the wait); both reset when the wait ends, however it ends. Like stock
`M109`/`M190`, the wait prints the heaters' temperature report line once a second.

### `dragonbreath/set_target` (Moonraker / API)
Moonraker publishes the klippy endpoint as JSON-RPC
`printer.dragonbreath.set_target` and HTTP `POST /printer/dragonbreath/set_target`:
```json
{"jsonrpc": "2.0", "method": "printer.dragonbreath.set_target",
 "params": {"target": 60}, "id": 1}
```
Applies the target exactly as `M141` does and answers once the device decides
(at most 5 s). It runs on the reactor, so it works while a print is blocked in
`M191`/`TEMPERATURE_WAIT`. Response:

| field | meaning |
|-------|---------|
| `accepted` | `true`, `false`, or `null` while `pending` |
| `pending` | `true` if the device had not answered within 5 s |
| `reason` | `null` when accepted; else a `protocol_error_code` value, `superseded` (a newer target replaced it) or `klipper_shutdown` |
| `seq` | intent sequence number of this request |
| `target` | the target that was requested |

`target` must be a JSON number (a quoted string is refused by Klipper).
A target outside the heater's `min_temp`/`max_temp` returns an error, as does a
request before Klipper has connected the heater hook (the target was not sent).

## Moonraker update manager
```ini
[update_manager dragonbreath-klipper]
type: git_repo
path: ~/dragonbreath-klipper
origin: https://github.com/plastikman/dragonbreath-klipper.git
primary_branch: main
managed_services: klipper
```

## Safety notes
This module is the *controller* side. The DragonBreath firmware enforces the real
safety limits (over-temp cutoffs, comms watchdog, target clamp). Read the
DragonBreath `docs/SAFETY.md`. The heater is mains-powered — supervise it.

## Credits
Klipper integration derived from Justin Hayes' pandabreath-klipper. Licensed under
GPL-3.0 (like Klipper and the upstream project — see [LICENSE](LICENSE)).
