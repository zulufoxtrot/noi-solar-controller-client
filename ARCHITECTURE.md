# Architecture — Noi Solar / Limu LTM2430

Date: 2026-08-02 · App: **Noi Solar 1.4.6 (build 49)** `com.wyadmin.solar.app` (Flutter, Dart AOT, iOS build running on Apple Silicon)
Vendor: Guangzhou NOI Technology / Limu Electronic (力牧科技, wyadmin.com)

## System diagram

```
                 ┌──────────────────────────────────────────────┐
                 │           lmsolar.wyadmin.com                │
                 │  nginx/1.24 + ThinkPHP V8.0.3 (PHP, Chinese) │
                 │                                              │
   HTTPS/JSON    │  REST API  ──────────────┐                   │
 ┌──────────────►│  /api/...                │  MQTT broker      │
 │               │                          │  :1883 (OPEN)     │
 │               └──────────────────────────┼───────────────────┘
 │                                            │        ▲
 │                                            │ Modbus │ MQTT (device
 │                                            │  RTU   │  connects +
 │               ┌────────────────────────────┘ frames │ subscribes)
 │               │ BLE GATT (local, no cloud)          │
 │               │ svc FFF1 (Modbus) + svc 00112233    │
 │               ▼                                     │
 │        ┌──────────────────────────────────────────────┐
 └───────►│  LTM2430 solar charge controller             │
   BLE    │  Wi-Fi+BLE MPPT, Modbus-RTU slave, addr 0xFF │
          └──────────────────────────────────────────────┘
```

## Channels

| Channel | Transport | Purpose | Auth |
|---------|-----------|---------|------|
| App ↔ Cloud | HTTPS REST + JSON envelope | account, binding, prefs, firmware check, **Modbus tunnel** (`/api/hmBridge`) | `token:` header (32-hex session) |
| Cloud ↔ Device | MQTT :1883 | telemetry push + remote command relay | per-device MQTT credentials (unknown) |
| App ↔ Broker | MQTT :1883 | realtime topics (`/charger/deviceInfo`, …) | per-user/device creds (unknown; NOT the account password) |
| App ↔ Device | BLE GATT | local monitoring/control, **Wi-Fi provisioning**, BLE-password-gated config | BLE password `000000` (user-set; factory default `666666`) |

## Key facts

* The device speaks **Modbus RTU on every channel**: BLE GATT, MQTT relay (`hmBridge`), presumably MQTT telemetry too. Same frame format everywhere: `[addr][FC][data][CRC16]`, address `0xFF` = "all-purpose single slave".
* The Flutter app has one Modbus client with two transports: `data/modbus/{modbus_client, ble_connector, http_connector, http_model}.dart`.
* The **firmware uses BIG-endian CRC** in frames (confirmed on-device and in the cloud-generated syncTime ack). The manual says low-byte-first — the manual is wrong for this firmware.
* Device identity: SN/node_id `000629252245`, model LTM2430, product_id 4 (= LTM-24xx Wi-Fi+BLE MPPT), BLE MAC `B4:C2:E0:E0:50:BC`, sw `1.0.8`, hw `1.3.2`, protocol `1.0.2`, manufacturer string "LimuTech".
* Account: `your@email.com`, user id 1771, user sn 17517695. Device id 2937, name "Colombis", read_interval 3000 ms, timezone Europe/Paris.

## Status as of 2026-08-10 evening

* Device is **online**: Wi-Fi connected (RSSI −54 dBm), MQTT connected + subscribed (regs 0x008A/0x008B = 1).
* BLE public registers readable without password. Config area gated; the Aug-02 "hung" state cleared with a power cycle.
* **BLE auth solved**: telemetry needs no PIN; config-area gate unlock = FC10 ASCII PIN @ `0x0400` (device answers `0x02` but honours it). PoC auto-unlocks (`BLE_PIN`). See `BLE.md`.
* Cloud tunnel `hmBridge` 500-crashes even with device online → server-side bug.
* ~17:15: device stopped answering Modbus over BLE entirely (accepts connections, stays silent). Suspected hung task. **Awaiting power cycle.**

## Files in this repo

| Path | Content |
|------|---------|
| `ARCHITECTURE.md`, `TODO.md`, `README.md` | big picture, open questions, quick start |
| `api/HTTP-API.md` | REST API reference (endpoints, envelope, tunnel) |
| `api/MQTT.md` | MQTT broker, topics, auth attempts |
| `bluetooth/BLE.md` | BLE GATT layout, Modbus-over-BLE, auth, tool usage |
| `rs485/PROTOCOL.md` | Modbus register map + framing reference |
| `rs485/solar charge controller modbus manual.pdf` | vendor protocol doc V1.0.0 (pages 13-14 missing) |
| `bluetooth/ble_modbus.py` | working BLE↔Modbus CLI tool (pyobjc/CoreBluetooth) |
| `bluetooth/ble_sniff.js` | Frida sniffer for the app's BLE traffic (needs root attach) |
| `bluetooth/` (app + Docker) | **dockerisable Python bridge** — BLE (bleak) → Modbus RTU → MQTT with HA auto-discovery (see `bluetooth/README.md`); `ble_modbus.py` tooling lives here alongside |
