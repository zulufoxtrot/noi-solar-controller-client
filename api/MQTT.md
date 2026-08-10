# MQTT channel — lmsolar.wyadmin.com:1883

The realtime channel between the cloud, the app, and the device. **Unencrypted TCP, port 1883** (8883/8083/8084 closed). Broker is open to the internet.

## Auth situation — UNSOLVED

* Anonymous connect → CONNACK rc=5 (not authorized).
* Account credentials (`your@email.com` / account password) → rc=5.
* API token as username/password → rc=5.
* ~8 derived combos (user sn, device sn, node_id, …) → rc=5.
* The login response contains **no MQTT fields** — the app does NOT receive broker credentials from the REST API.
* The app binary contains only config **field names**: `mqttAddress`, `mqttPort`, `mqttClientId`, `mqttUsername`, `mqttDebug` — no values. These are almost certainly **provisioned to the device over BLE** (Wi-Fi setup flow) and live in device config registers; the app likely reads them from the device or derives them per device.

Conclusion: credentials are per-device (or per-device-class) and the realistic recovery paths are (a) read the device config registers once BLE auth is solved, or (b) sniff the device's plaintext MQTT session on the LAN (see TODO.md).

## Topics (from app binary)

```
/charger/deviceInfo
/cloud/device/unbind
/device/info/desc
/firmware/update/notice
/firmware/updater
```

The prefixes `/charger`, `/cloud`, `/firmware` exist as standalone strings; full topics likely embed product/node ids (e.g. `/charger/<sn>/deviceInfo`). App UI shows "Subscribe success/failed" events.

Payload format: unverified (never got an authenticated session). Given everything else, most likely the same Modbus RTU frames and/or JSON status pushed at `read_interval` (3000 ms in the device record).

## Device-side status (readable via BLE, no password)

| Register | Meaning | Live value 2026-08-02 ~16:30 |
|----------|---------|------------------------------|
| 0x0088 | Wi-Fi: high byte = signal (signed dBm), low byte = linkage | `0xCA01` → −54 dBm, connected |
| 0x008A | Server (MQTT) connection 0/1 | **1 = connected** |
| 0x008B | MQTT subscription 0 failed / 1 ok | **1 = ok** |

Device event-log codes: 132/133 = MQTT connect fail/ok, 134/135 = MQTT subscribe fail/ok (logs at registers 0x0200+).

## Notes

* Device went from cloud-offline to online during the session (~16:20→16:27); the REST device record carries no online flag — presence is only visible via MQTT or the device registers.
* `hmBridge` (the REST→MQTT Modbus relay) 500-crashes even with the device online: the server-side relay is broken independently of device state.
* The app on this Mac had no open :1883 socket while idle — it connects to the broker only when needed (or never got creds without a device session).
