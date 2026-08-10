# Open questions & next avenues

Status: 2026-08-10 evening. Legend: 🔴 blocked · 🟡 in progress · 🟢 doable now

## Immediate blocker

* ✅ **Resolved 2026-08-10 by a power cycle** (battery/PV disconnect-reconnect). The device answers Modbus over BLE again; live register dump (0x00A0/0x0080/0x000A) verified on the Mac and the PoC BLE→MQTT→HA path runs. Lesson: the 00112233 UART service is fragile — one payload per connection, watch for side effects; a hung link clears on power cycle.

## 1. BLE auth — ✅ SOLVED (2026-08-10)

* Working PIN: **`000000`** (user-set; factory default `666666`).
* Mechanism: telemetry (`0x00A0`, `0x0080`, sysinfo `0x000A`) needs NO PIN. The config area (`0x0400+`) is gated (EXC `0x04`); unlocking = FC10 ASCII PIN (2 chars/reg) written to `0x0400`. The device replies EXC `0x02` to that write yet honours it. Unlocked state persists across reconnects; resets on power cycle.
* PoC `bluetooth/app` auto-unlocks on EXC `0x04` (env `BLE_PIN`, default `000000`). See `bluetooth/BLE.md`.
* Remaining (🟢 doable): map the now-readable config registers (0x0400+) — charge parameters, Wi-Fi/MQTT provisioning; and verify the app's own frame with `ble_sniff.js` as ground truth.

## 2. MQTT credentials

* 🔴 Broker rejects anonymous + all tried combos (rc=5). Creds are per-device, provisioned via BLE; not in the REST API, not hardcoded in the binary. Avenues:
  1. 🟡 Read device config registers once BLE auth works (most direct).
  2. 🟢 **LAN sniffing**: the device talks plaintext MQTT on the user's LAN. Capture its CONNECT (contains username+password in cleartext) via:
     * router packet capture / mirror port (if the router supports it),
     * ARP-spoof the gateway from the Mac (needs root; `sudo` + scapy/arpspoof),
     * or point the device at a temporary open AP and sniff there.
     The device reconnects on its own schedule (broker timeout / Wi-Fi flap / reboot) — a capture running across a reconnect gets the CONNECT packet. Even without CONNECT, telemetry PUBLISH packets reveal exact topics + payload format.
  3. 🟡 Check whether the vendor's firmware images embed a default MQTT account (see §3).

## 3. Firmware

* 🟢 `/api/firmware/check?product_sn=4` exists (500s when no records). Watch it / try other product_sns; if a firmware file becomes downloadable, extract: default MQTT creds, topic templates, auth register map, upgrade format. Also relevant to the 00112233 service (likely DFU/console).

## 4. Cloud tunnel (hmBridge)

* 🔴 500-crashes even with device MQTT-online → server-side bug (null relay channel), not device state. Avenues:
  1. 🟢 Retry periodically / after app session activity; the relay may be established lazily (e.g., when the app subscribes to the device topic).
  2. 🟡 Compare with the web console `/pc` — its JS may call the bridge differently (extra fields, different node id form).
  3. 🟡 `control/syncTime` works and returns a device-shaped ack — either the device really acks (then SOME relay path works!) or the server fabricates it. Test: syncTime, then immediately BLE-read 0x0504 and see if the RTC moved. That distinguishes a live relay from a fake ack.

## 5. Misc open items

* 🟡 UART service 00112233: after device recovery, try Modbus frames on it (not just ASCII), one connection at a time.
* 🟡 0x0087 semantics: 1 vs 3 (link vs app) — correlate with app connections.
* 🟡 SOC scaling: 0x00AB read 100 — ×0.1 % ⇒ 10 % seems wrong vs 14 V battery; verify against app display.
* 🟡 Device Wi-Fi MAC unknown (`wifi_mac` empty in cloud) — needed for LAN-sniff filtering; get from router DHCP list or a register.
* 🟢 Long-term: once BLE auth + MQTT creds are solved, write a small client lib (Python) for local + cloud monitoring/control, and document the config-area register map for the community.
* ✅ **PoC built + tested remotely**: `bluetooth/` = dockerisable Python bridge (bleak) reading the verified public registers over BLE and publishing to MQTT with HA auto-discovery. Uploaded to `youri@10.6.0.3:~/reverse-noi-solar/poc`; **SIMULATE path verified end-to-end on that ARM SBC** (18 entities discovered by a local broker, retained state + LWT correct). Live BLE first-read still blocked by the hung device above. `SOC_SCALE` default is ×1 (raw 100 ⇒ 100 %) pending app cross-check.
