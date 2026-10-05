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
  reconnect attempts back off while state polling continues.
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

This config is **printer-agnostic** — it works on any Klipper machine (Voron,
RatRig, a stock printer, the Snapmaker U1/PAXX, …), since the helper talks to the
DragonBreath device over the LAN, not to the printer's MCU. The only printer the
`[dragonbreath]` + `[heater_generic]` sections must *not* be duplicated on is one
whose firmware already ships them (e.g. the Snapmaker U1/PAXX managed config) —
there, include them once.

### Chamber macros (M141 / M191)
`M141` (set chamber) and `M191` (set + wait) are not native Klipper. With
`register_macros: True` (the default) the module registers both for you:
- `M141 S<t>` — set the chamber target (`S0` = off), returns immediately.
- `M191 S<t>` — set the target and **block until the chamber reaches `S`**.

To define your own instead — e.g. to add a **release tolerance** so a slow PTC
chamber's on/off undershoot can't hang the wait forever, or on a printer that
already defines these commands — set `register_macros: False` and add:

```ini
[gcode_macro M141]
description: Set Chamber Temperature
gcode:
    {% set s = params.S|default(0)|float %}
    SET_HEATER_TEMPERATURE HEATER="dragonbreath" TARGET={s}

[gcode_macro M191]
description: Wait for Chamber Temperature (R = release temp, defaults to S-5)
gcode:
    {% set s = params.S|default(0)|float %}
    {% set r = params.R|default(0)|float %}
    {% if r <= 0 %}{% set r = s - 5 %}{% endif %}
    {% set r = [r, s]|min %}
    M141 S{s}
    TEMPERATURE_WAIT SENSOR="heater_generic dragonbreath" MINIMUM={r}
```

`M191` here starts the heater (`M141 S`) and waits until the `heater_generic
dragonbreath` sensor reaches `R`, the **release temperature** — default `S-5`,
clamped so it can never exceed `S`. The small margin releases the print once the
chamber is *near* target instead of stalling if it asymptotes a degree or two
short. Pass `R` explicitly to override (e.g. `M191 S55 R52`). `M191 S0` turns the
heater off and returns immediately. Note the sensor is the DragonBreath device's
own NTC (in the heater outlet, so it reads a little ahead of the bulk chamber) —
for a guaranteed bulk-chamber **soak**, follow the wait with a timed dwell (`G4`)
or a soak macro in your print-start sequence.

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
`device_moonraker_connected`, and `protocol_error`.

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
