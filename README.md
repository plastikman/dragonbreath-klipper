# openbreath-klipper

Klipper `extras` module that surfaces an **[OpenBreath](https://github.com/plastikman/OpenBreath)**-firmware
BIGTREETECH Panda Breath as a standard chamber heater — live temperature + a
settable target in **Fluidd/Mainsail**, driven with **M141 / M191**.

It talks to OpenBreath's HTTP control API over your LAN (no MQTT broker, no cloud).

> Focused fork of Justin Hayes' [pandabreath-klipper](https://github.com/justinh-rahb/pandabreath-klipper).
> It keeps the proven Klipper glue (sensor factory + virtual pin → `heater_generic`,
> the reactor-poll sensor feed, and the fail-safe force-off) and drops the stock
> firmware's WebSocket/MQTT transports and work-mode/drying/filament machinery,
> which OpenBreath doesn't use. The transport is a small stdlib HTTP client.

## How it works
- Registers a `sensor_type: openbreath` + a `heater_pin: openbreath:pwm` virtual
  pin, so a normal `[heater_generic]` renders as a chamber heater card in Fluidd.
- A reactor timer polls `GET /status` and feeds the temperature into the heater's
  sensor callback (on the MCU clock, so `verify_heater` works).
- Setting the heater target (M141 / `SET_HEATER_TEMPERATURE` / the Fluidd card)
  is pushed to the device with `POST /target?t=<C>`.
- While a target is set, the module `POST /heartbeat`s every poll. If Klipper
  crashes/hangs the heartbeats stop and OpenBreath's own comms watchdog latches
  the heater off. On Klipper disconnect/shutdown the module also force-offs the
  device, and it force-offs any *uncommanded* device heating.
- Every mutating request carries the `X-OpenBreath-Auth` header.

## Install
```bash
cd ~
git clone https://github.com/plastikman/openbreath-klipper
cd openbreath-klipper
./install.sh
```
`install.sh` symlinks `openbreath.py` into `~/klipper/klippy/extras/` and restarts
Klipper. Add the Moonraker update-manager block below to get updates in Fluidd.

## Configuration
```ini
[openbreath]
host: 10.168.2.53          # OpenBreath device IP or hostname (openvent.local also works)
#port: 80
#token: web                # X-OpenBreath-Auth value. Leave as "web" unless you set
                           # a control token on the device (NVS ctl_token); then match it.
#poll_interval: 2.0        # seconds between /status polls
#register_macros: True     # register M141/M191 (set False if you define your own)

[heater_generic openbreath]
heater_pin: openbreath:pwm
sensor_type: openbreath
control: watermark
max_delta: 2.0
min_temp: 0
max_temp: 75               # OpenBreath firmware hard-caps the target at 70 C

[verify_heater openbreath]
check_gain_time: 300       # PTC chamber heaters are slow — be generous
hysteresis: 5
heating_gain: 1
```
The `[openbreath]` name and the `[heater_generic openbreath]` name must match.

## Usage
- **Fluidd/Mainsail:** the `openbreath` heater appears with the other heaters —
  set the target from its card.
- **G-code:**
  - `M141 S45` — set chamber to 45 °C (0 = off)
  - `M191 S45` — set chamber to 45 °C and wait until reached (use in print start)
  - `SET_HEATER_TEMPERATURE HEATER=openbreath TARGET=45`
  - `OPENBREATH_RESET` — clear a latched device fault (over-temp / sensor / comms)

Extra status is exposed on the `openbreath` printer object for macros/dashboards:
`heating`, `fault`, `fault_reason`, `ptc_temp`, `connected`.

## Moonraker update manager
```ini
[update_manager openbreath-klipper]
type: git_repo
path: ~/openbreath-klipper
origin: https://github.com/plastikman/openbreath-klipper.git
primary_branch: main
managed_services: klipper
```

## Safety notes
This module is the *controller* side. The OpenBreath firmware enforces the real
safety limits (over-temp cutoffs, comms watchdog, target clamp). Read the
OpenBreath `docs/SAFETY.md`. The heater is mains-powered — supervise it.

## Credits
Klipper integration derived from Justin Hayes' pandabreath-klipper. MIT licensed.
