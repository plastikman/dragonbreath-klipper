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
- **All network I/O runs on a single background worker thread** — Klipper's
  reactor never blocks on the device. Setting the heater target
  (M141 / `SET_HEATER_TEMPERATURE` / the Fluidd card) just hands the *desired*
  target to the worker and returns immediately; the worker POSTs `/target?t=<C>`,
  retrying with backoff until the device accepts it. (This is deliberate: a
  synchronous HTTP write on the reactor could stall it long enough to trip
  "Timer Too Close" and shut the MCU down mid-print.)
- The worker also polls `GET /status` and feeds the temperature into the heater's
  sensor callback (on the MCU clock, so `verify_heater` works). Both the
  Klipper-desired target and the device-reported target are exposed on the
  `dragonbreath` object (`target` vs `device_target`) so divergence is visible.
- While a target is set, the worker `POST /heartbeat`s every poll. If Klipper
  crashes/hangs the heartbeats stop and DragonBreath's own comms watchdog latches
  the heater off — **that watchdog, not the OFF request, is the real fail-safe.**
  On Klipper disconnect/shutdown the module commands the device off (delivered by
  the worker) and force-offs any *uncommanded* device heating.
- Every mutating request carries the `X-DragonBreath-Auth` header.

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
#poll_interval: 2.0        # seconds between /status polls
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

## Usage
- **Fluidd/Mainsail:** the `dragonbreath` heater appears with the other heaters —
  set the target from its card.
- **G-code:**
  - `M141 S45` — set chamber to 45 °C (0 = off)
  - `M191 S45` — set chamber to 45 °C and wait until reached (use in print start)
  - `SET_HEATER_TEMPERATURE HEATER=dragonbreath TARGET=45`
  - `DRAGONBREATH_RESET` — clear a latched device fault (over-temp / sensor / comms)

Extra status is exposed on the `dragonbreath` printer object for macros/dashboards:
`heating`, `fault`, `fault_reason`, `ptc_temp`, `connected`.

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
Klipper integration derived from Justin Hayes' pandabreath-klipper. MIT licensed.
