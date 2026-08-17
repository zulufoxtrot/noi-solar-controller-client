# BLE channel — LTM-252245

Local link between app and controller. Carries the same Modbus RTU frames as the cloud tunnel, plus a second UART-ish service. BLE password: **`000000`** (user-set; factory default is `666666` — verified working 2026-08-10).

## Discovery & connection

* Advertises as **`LTM-252245`** (pattern `LTM-<last 6 of SN>`), RSSI ≈ −66…−84 dBm at the Mac.
* CoreBluetooth UUID on this Mac: **`1FBE6253-5026-BD17-135C-F937BEEB5992`** — stable across sessions. Skip scanning: `retrievePeripheralsWithIdentifiers:` → `connectPeripheral:` connects in <1 s.
* The device stops advertising while connected to a central (single link).
* **Not advertising to new clients? Unpair it from its WiFi network first** (not Bluetooth — its own Wi-Fi pairing). Confirmed 2026-08-16: after power cycles and long quiet windows the controller kept refusing to advertise to a new central (the vendor app still connected via its cached accept-list entry, but fresh scans saw nothing). Unpairing the controller from the WiFi network makes it advertise again for new clients. Check this BEFORE the "power-cycle / hung state" fallback.
* `retrieveConnectedPeripheralsWithServices:` shows the peripheral (svc 00112233) even when another app (the Noi Solar Mac app) holds/holds-no active link.

## GATT layout (verified empirically — ⚠️ roles are opposite of the names)

| Service | Characteristic | Props | Role |
|---------|---------------|-------|------|
| `0000FFF1-…` (Modbus tunnel) | `0000AAA1` | 0x10 | **NOTIFY** — device→client responses |
| | `0000BBB1` | 0x04 | **WriteWithoutResponse** — client→device requests |
| `00112233-4455-6677-8899-AABBCCDDEEFF` (secondary) | `00112333-…` | 0x10 | NOTIFY |
| | `00112433-…` | 0x04 | WriteWithoutResponse |

Subscribing to a write-only char fails with `CBATTErrorDomain 6 "request is not supported"` — a reliable way to identify roles.

## Modbus over BLE (service FFF1)

* Write the full RTU frame to **BBB1**, read the response from **AAA1** notifications (may arrive fragmented; accumulate by expected length: FC 0x03 → `3+bytecount+2`, exception → 5, write ack → 8).
* Device address `0xFF`. **CRC16-Modbus stored BIG-endian** (firmware contradicts the manual — confirmed both by device responses and by the cloud syncTime frame `FF 10 05 04 00 04 19 95`).
* **Reads of public areas need NO password.** Verified readable:
  * System info 0x000A–0x0027 (Product ID 4, sw 1.0.8, hw 1.3.2, protocol 1.0.2, SN, "LimuTech")
  * System config 0x0050–0x0057 (80 V max PV / 36 V max batt / 30 A charge / 30 A load / 1 battery / capability bitfields)
  * Connect data 0x0080–0x008B (incl. Wi-Fi/MQTT status — see `../api/MQTT.md`)
  * Running data 0x00A0–0x00AF (state 5=running, PV 21.09 V/0.3 A/7.6 W, battery 0x8004=lithium×4 strings, 14.00 V, charge phase=fast)
  * RTC 0x0504–0x0507 → `[year][month<<8|day][hour<<8|min][sec<<8|0]` — matched wall clock exactly
* **Block reads fail entirely if ANY register in the range is invalid** (exception 0x02) — read exact documented ranges only.
* The **config area (0x0400–0x0480+) is gated**: reads return exception `0x04` (operation failed), NOT `0x08` (wrong password). Unlocking: see "BLE auth" below.

## Connection behavior

* Idle connections survive indefinitely (tested 8 s+; earlier "4 s watchdog" hypothesis is wrong).
* **Repeated 0x04 errors trigger a forced disconnect after ~4 s** (error-rate triggered kick). Interleave probes with valid reads, or keep error bursts short.
* 0x0087 (BT connection status) read `1` (link) in early sessions, later `3` (app) — possibly escalates after traffic; exact semantics unconfirmed. 0x0086 flipped 0→1 (slave).

## BLE auth — SOLVED for telemetry; config gate mechanism confirmed

* Password is **`000000`** (user-set; factory default **`666666`**).
* **Telemetry/config-header reads need NO PIN** on a fresh link: running data (`0x00A0`), connect data (`0x0080`), sysinfo (`0x000A`) all read fine immediately after connect. Observed live at 21:50 on 2026-08-10: `0x00A0` → `[5, 0,0,0,0, 0,0,0, 0x8004, 1, 0x52E, 0x55, 0x100, 0xF7, 0xFFFB, 0xFFB9]` = state 5 (running), battery type 0x8004, rated 1, **battery 0x52E ≈ 13.27 V**, SOC 0x55 = 85 %, charge 2.47 A, night-time PV 0 — decoded live, no auth step.
* The **config area (0x0400–0x0480+) is gated**: reads return exception `0x04`.
* **Presenting the PIN** = FC 0x10, ASCII PIN packed 2 chars per register (regs `[0x3030, 0x3030, 0x3030]` for `"000000"`), written to **`0x0400`**. The device answers the write itself with **exception `0x02`** (illegal address) while still accepting it — treat the `0x02` response as success, not a failure. Verified: after that write, gated/telemetry reads succeed from the same link. Frame: `FF 10 04 00 00 03 06 30 30 30 30 30 30 20 FB`.
  * The unlocked state **persists across BLE reconnects**; it resets only on a power cycle.
* The app has `_sendPassword` (in `usb_settings_viewmodel.dart` context) and a "Bluetooth Password" settings UI; the controller's fault bitmap/event log even carry "wrong password" flags (event 128, byte2 bit). Ground truth of the app's exact frame: `ble_sniff.js` Frida hook (needs root).
* PoC: `app/` presents the PIN as the **first Modbus frame after every connect** — FC10 ASCII PIN to `0x0400` (`BLE_PIN` env, default `000000`) — because the controller re-gates the link on (re)connects and drops links that don't present the PIN promptly (observed: every connection dropped ~5–6 s in with no Modbus traffic, even from plain `bluetoothctl`). The device answers the write with `0x02`/`0x04` (or nothing) yet still honours it, so any response is success; the unlock persists across reconnects so the write is idempotent. A lazy re-present remains as a backstop if a read is still gated.

## ✅ Blocker cleared 2026-08-10 (power cycle)

The Aug-02 "hung state" was resolved by a power cycle (battery/PV disconnect-reconnect). Live GATT telemetry is fully verifiable again from the Mac: full register dump of 0x00A0/0x0080/0x000A decodes correctly (see above). The SBC (colombis, BlueZ) also connects fine; the earlier "Request attribute has encountered an unlikely error" was the hung link, not the stack.

## Reconnecting after a container restart (stale BlueZ link)

* Because the bridge runs in a container that shares the **host's BlueZ** (`/var/run/dbus`), a HCI connection is owned by the host daemon. If the previous process died without disconnecting (hard restart, killed mid-call, D-Bus disconnect stalling past the timeout), BlueZ **keeps the link up even after the container restarts**.
* This device is **single-link and stops advertising while connected**, so a leftover link makes it invisible to scans: the bridge loops on `no controller found` even though the controller is fine.
* The bridge now recovers automatically: before every scan it asks BlueZ to release the controller by address (`org.bluez.Device1.Disconnect`), and only after 10+ failed scans (the controller has been invisible for a long time) does it escalate to `org.bluez.Adapter1.RemoveDevice` (forgets the device — but not on mere connect failures, since a cold service discovery is what the slow controller cannot survive). Expect at most one release + one scan cycle (~15–40 s) of downtime after an unclean restart.
* If the controller still never advertises after repeated releases, check whether it's paired to a WiFi network — **unpair it from WiFi to make it advertise for new clients again** (confirmed 2026-08-16, see "Discovery & connection"). Only if that doesn't help, fall back to the firmware "hung state" fix: power-cycle the controller (disconnect/reconnect battery or PV). The bridge logs a warning telling you exactly that after ~6 failed scans.
* **Hung GATT variant (Aug-11)**: the controller advertises and accepts connections, but its GATT stack never answers service discovery (no GATT service objects appear in the BlueZ tree), and the link dies ~5 s in (supervision timeout) — `failed to discover services, device disconnected`. Reproducible from plain `bluetoothctl` and with bleak, so it is not client-side. Same fix: power-cycle the controller. The bridge's eager-PIN + GATT-cache-clearing recovery then reconnects automatically once the controller is healthy.

## Tooling — `ble_modbus.py` (this repo)

```
python ble_modbus.py scan 0x0000 0x0100 0x10   # block scan, auto-reconnect
python ble_modbus.py read 0x00A0 1             # single FC03 read
python ble_modbus.py multi "0x0080:12,0x0504:4"
python ble_modbus.py idle / sniff
python ble_modbus.py auth2                     # FC10 ASCII PIN write probes
python ble_modbus.py auth_probe                # FC06 probe hunt for the auth register
```

Uses pyobjc + CoreBluetooth; venv at `/var/folders/6_/qzsl7_sx22194131c5cqvz400000gn/T/opencode/pdfenv/bin/python`.

`ble_sniff.js` — Frida script hooking `CBPeripheral writeValue:forCharacteristic:type:` + the flutter_blue_plus notification delegate; captures the app's exact auth frame. Attach needs root: `sudo frida -p <pid of Noi Solar> -l ble_sniff.js`.
