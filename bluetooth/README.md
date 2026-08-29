# PoC: Limu/NOI solar charge controller → BLE → MQTT → Home Assistant

Bridge that reads telemetry from the Limu/NOI **LTM** solar
charge controller  and publishes it to an MQTT broker with Home Assistant auto
discovery enabled.

## Quick start

### Native

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
public register blocks, and publishes.

### Docker

Build locally:

```bash
cd bluetooth/
docker compose up -d --build
```

Or use the prebuilt image:

```bash
docker run -d --name limu-solar --network host --restart unless-stopped \
  -v /var/run/dbus:/var/run/dbus:ro \
  -e MQTT_HOST=192.168.1.10 \
  ghcr.io/zulufoxtrot/noi-solar-controller-client:v0.1.0
```

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
| `BLE_PIN` | empty | optional unlock PIN (FC10 @ `0x0400`); **leave empty on fw ≥ 2.0.4** — there a PIN write makes the controller drop the BLE link within ~2 s |
| `SCAN_TIMEOUT` | `35` | discovery scan duration (s); must exceed the controller's ~25 s advert interval |
| `READ_TIMEOUT` | `5` | per-read response timeout (s) |
| `BLE_TIMEOUT` | `10` | BLE connect timeout (s) |
| `POLL_INTERVAL` | `30` | telemetry poll interval (s) |
| `RETRY_INTERVAL` | `15` | reconnect backoff base (s) |
| `RETRY_BACKOFF_MAX` | `300` | backoff cap between retries (s) |
| `MAX_SESSION_SECONDS` | `75` | proactive link rotation; `0` disables |
| `ROTATE_GAP_SECONDS` | `20` | quiet gap after a planned rotation before reconnecting |
| `POST_CONNECT_SECONDS` | `8` | settle delay before the first read after connect |
| `BURST_MODE` | `false` | capture one sample per connection, then leave immediately |
| `DEVICE_NAME` | `Limu Solar Controller` | HA device display name |
| `SIMULATE` | `false` | fake telemetry, no BLE |
| `LOG_LEVEL` | `INFO` | |

## Telemetry

> **fw ≥ 2.0.4 reality (overrides older text below):** every BLE connection
> must open with a sysinfo-area read (session handshake) or the controller
> gates all further traffic with EXC `0x04`. Links die within ~90-100 s, so
> production polls in **burst mode**: connect → opener → vendor-cadence small
> reads (max 4 registers each, never reorder that sequence) → optional
> extension/statistics/link blocks → publish → disconnect. The external
> temperature input has no sensor fitted (register `0xB9` reads `0xFFFF`);
> its entity was removed and old discovery/state topics are cleared with
> empty retained payloads.

Blocks covered per burst:

| Block | Registers | Values |
|-------|-----------|--------|
| System info | `0x000A–0x0027` | model, SW/HW versions, serial, manufacturer (opener + once for HA device) |
| Running data | `0x00A0–0x00AF` | state, fault, **PV V/A/W**, battery V/SOC/type, charge phase/V/A/W |
| Connect data | `0x0080–0x008B` | Wi-Fi RSSI + link, cloud-MQTT link status |
| Extension | `0x00B0–0x00BB` | load/USB switches+V/A/W, controller temp, fan |
| Statistics | `0x0300–0x0313` | runtime, totals, today's generation/consumption/maxima |

Optional groups are all-or-nothing per burst: a failed group keeps HA's last
retained values instead of publishing zeros.

Published as retained per-key state topics under `MQTT_TOPIC_PREFIX`
(default `limu_solar`; production overrides to `noi_solar`), plus a retained
availability topic (LWT `offline`):

```
noi_solar/<sn>/pv_voltage      → 22.31
noi_solar/<sn>/availability    → online / offline
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
  up on MQTT and the sensor entities keep their last retained values
  (availability tracks the *bridge process*, not the BLE link — BLE failures
  never flip it). The pairing switch has its own availability
  (`availability_bridge`) so it can be flipped back on. Unpairing is purely
  dropping the central BLE link — no OS-level bond removal.

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
* The config (`0x0400+`) can be password-gated: gated reads return exception
  `0x04`. **fw ≥ 2.0.4 override:** the gate is armed by *skipping the sysinfo
  session opener*, not by reconnects per se — see Telemetry above. The PIN is
  presented lazily (FC10 ASCII to `0x0400`) only when a read comes back gated;
  in a correctly-opened session the write is rejected with `0x02`/`0x04` yet
  honoured, and the link survives. `BLE_PIN` defaults to `000000`; leave it
  unset only if you enjoy lockouts.

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
