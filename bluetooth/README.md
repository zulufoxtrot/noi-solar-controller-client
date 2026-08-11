# PoC: Limu/NOI solar charge controller → BLE → MQTT → Home Assistant

Proof-of-concept bridge that reads telemetry from the Limu/NOI **LTM** solar
charge controller (Modbus RTU spoken over the BLE GATT "Modbus tunnel", service
`0000FFF1`) and publishes it to an MQTT broker with **Home Assistant auto
discovery** enabled.

Transport: `bleak` (CoreBluetooth on macOS, BlueZ/D-Bus on Linux). The same code
runs natively on a Mac for development and inside a Linux container (e.g. a
Raspberry Pi / home server) for production.

```
LTM-252245 (BLE GATT FFF1)  ──Modbus RTU──▶  this bridge  ──MQTT──▶  Home Assistant
```

## Quick start

### Native (macOS — dev / no BLE in Docker Desktop)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# without hardware — exercises the MQTT/HA path with fake telemetry:
SIMULATE=1 MQTT_HOST=192.168.1.10 .venv/bin/python -m app

# with your controller on the LAN:
MQTT_HOST=192.168.1.10 .venv/bin/python -m app
```

The bridge scans for any BLE device advertising a name starting with `LTM-`
(controllers advertise `LTM-<last 6 of SN>`), connects, reads the verified
public register blocks, and publishes. Point `CONTROLLER_ADDRESS` at the MAC to
skip scanning.

### Docker (Linux host with Bluetooth + BlueZ)

```bash
docker compose up -d --build
```

`docker-compose.yml` uses `network_mode: host` and mounts the host D-Bus socket
(`/var/run/dbus`) so the container reaches both the Bluetooth adapter and your
broker. **Docker Desktop on macOS cannot pass Bluetooth through** — run
natively there.

If a **container restart** leaves the bridge looping on `no controller found`:
the previous process died while the host BlueZ still held the BLE link (the
controller is single-link and stops advertising while connected). The bridge now
auto-recovers — it releases/removes the stale BlueZ device before scanning — but
if the controller still never shows up after repeated releases, power-cycle it
(see `BLE.md` → "Reconnecting after a container restart").

## Configuration (env vars)

| Variable | Default | Meaning |
|----------|---------|---------|
| `MQTT_HOST` | `127.0.0.1` | MQTT broker host |
| `MQTT_PORT` | `1883` | MQTT broker port |
| `MQTT_USERNAME` / `MQTT_PASSWORD` | empty | broker credentials (optional) |
| `MQTT_TOPIC_PREFIX` | `limu_solar` | state topic root |
| `MQTT_DISCOVERY_PREFIX` | `homeassistant` | HA discovery root |
| `MQTT_CLIENT_ID` | `limu-solar-poc` | MQTT client id |
| `CONTROLLER_NAME_PREFIX` | `LTM-` | advertised-name filter for auto-discovery |
| `CONTROLLER_ADDRESS` | empty | controller MAC/UUID → skip scanning |
| `BLE_ADAPTER` | empty | e.g. `hci0` (Linux/BlueZ only) |
| `BLE_PIN` | `000000` | unlock PIN; presented as ASCII via FC10 to `0x0400` immediately after every connect |
| `SCAN_TIMEOUT` | `20` | discovery scan duration (s) |
| `READ_TIMEOUT` | `5` | per-read response timeout (s) |
| `BLE_TIMEOUT` | `10` | BLE connect timeout (s) |
| `POLL_INTERVAL` | `30` | telemetry poll interval (s) |
| `RETRY_INTERVAL` | `15` | reconnect backoff (s) |
| `DEVICE_NAME` | `Limu Solar Controller` | HA device display name |
| `SIMULATE` | `false` | fake telemetry, no BLE |
| `LOG_LEVEL` | `INFO` | |

## Telemetry

Reads three verified public register blocks (see `../rs485/PROTOCOL.md`):

| Block | Registers | Values |
|-------|-----------|--------|
| System info | `0x000A–0x0027` | model, SW/HW versions, serial, manufacturer (once, for HA device) |
| Running data | `0x00A0–0x00AF` | state, fault, **PV V/A/W**, battery V/SOC/type, charge phase/V/A/W |
| Connect data | `0x0080–0x008B` | Wi-Fi RSSI + link, cloud-MQTT link status |

Published as retained per-key state topics, plus a retained availability topic
(LWT `offline`):

```
limu_solar/<sn>/pv_voltage    → 21.09
limu_solar/<sn>/availability  → online / offline
```

The `battery_soc` entity ("Battery") also carries JSON attributes on
`limu_solar/<sn>/battery_soc/attributes` (battery_soc, battery_voltage,
battery_charge_power/voltage/current), so the battery info lives on one entity
while the individual sensors remain available too.

Home Assistant auto discovery registers 44 entities (sensors + binary sensors)
under the configured discovery prefix, grouped into a single device keyed by the
controller serial number, plus a **BLE Pairing switch** (see below). The Load,
USB and Fan switches are writable: HA toggles publish to `<prefix>/<sn>/<key>/set`
and the bridge writes the corresponding controller register. State is
`stat_cla: measurement`, device classes and units set per HA conventions
(e.g. `dev_cla: battery` for SOC, `dev_cla: energy` for the kWh counters).

## BLE pairing toggle (Home Assistant switch)

Auto-discovery also exposes a `switch` entity ("BLE Pairing") so you can release
the controller to other clients (e.g. the phone app) without stopping the bridge:

* `ON` — the bridge connects (pairs) to the controller and polls telemetry.
* `OFF` — the bridge drops the BLE link and holds off. The controller is
  single-link: while a central is connected it stops advertising, so dropping the
  link makes it re-advertise and lets any other client connect. The bridge stays
  up on MQTT, the sensor entities go unavailable (`availability = offline`), and
  the switch keeps its own availability (`availability_bridge`) so it can be
  flipped back on. Unpairing is purely dropping the central BLE link — no
  OS-level bond removal.

New topics:

```
limu_solar/<sn>/pairing               → paired / unpaired   (switch state)
limu_solar/<sn>/pairing/set           ← paired / unpaired   (switch command)
limu_solar/<sn>/availability_bridge   → online / offline    (switch availability)
```

## Protocol facts baked in (verified against the device)

* Slave address `0xFF`, **CRC16-Modbus stored big-endian** (firmware contradicts
  the vendor manual — see `../rs485/PROTOCOL.md`).
* GATT roles are opposite of the characteristic names: `AAA1` = NOTIFY
  (responses), `BBB1` = WriteWithoutResponse (requests).
* Responses may arrive fragmented — accumulated by expected frame length.
* Only the exact documented public ranges are read as blocks; a block read fails
  entirely (exception `0x02`) if any register in it is invalid.
* The config (`0x0400+`) and — on reconnects — the whole link are password-gated:
  reads return exception `0x04`. The bridge **presents the PIN lazily** — once,
  when a read actually comes back gated — by writing the ASCII PIN to `0x0400`
  via FC10 (the device replies `0x02`/`0x04` to that write yet honours it). It
  avoids the eager write so a fresh link never trips the device's error-rate
  kick. `BLE_PIN` overrides the default `000000`.

## Open items

* **Live first-read*/auth-gated-*read**: the full BLE→MQTT→HA path plus live
  telemetry was verified on the Mac on 2026-08-10 (the Aug-02 "hung" state
  cleared with a power cycle, see `BLE.md`). The auto-unlock path is covered
  by a unit-level fake; final confirmation on the SBC after next deploy.
* **SOC scaling**: register `0x00AB` reads a raw `100` while the battery was
  effectively full (14.0 V / 4S lithium), so `SOC_SCALE = 1.0` is used (100 %)
  despite the manual saying ×0.1. Flip `SOC_SCALE` in `app/registers.py` if the
  official app disagrees.
* No writes are implemented yet (RTC sync, load/USB switch, charge params) —
  these need the config-area gate, which now has a confirmed unlock path
  (see `BLE.md`), but the register map for charge params is still unknown.

## Layout

```
Dockerfile, docker-compose.yml, requirements.txt
app/
  main.py        orchestration: discover → poll → publish, reconnect loop
  ble_client.py  bleak transport + controller auto-discovery (+ SIMULATE fake)
  modbus.py      RTU framing: CRC16, build/parse, exception handling
  registers.py   verified register map + decoders
  mqtt_client.py HA discovery + retained state/availability publishing
  config.py      env-var configuration
```
