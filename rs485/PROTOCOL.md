# Modbus protocol reference — Limu/NOI solar controllers

Framing, function codes, exception codes and the register map. Consolidates the vendor manual ("Solar Controller Communication Protocol V1.0.0", Limu, 2026-03) with on-device verification (2026-08-02). See ARCHITECTURE.md for the big picture, `bluetooth/BLE.md` / `api/HTTP-API.md` / `api/MQTT.md` for the transports.

## Framing

* Frame: `[Device Address 1B][FC 1B][Data NB][CRC16 2B]`, max PDU 256 B.
* Address: `0x01–0xF7` normal, `0x00` broadcast (no reply), **`0xFF` = all-purpose single-slave** (used by app & cloud).
* Registers 16-bit **big-endian**; 32-bit values = high word at lower address.
* CRC16-Modbus (poly 0xA001, init 0xFFFF) over address..data. **This firmware stores the CRC BIG-endian** (manual says low-byte-first — wrong; confirmed on-device and via the cloud syncTime frame).
* Max 256 B/PDU; serial (if direct): 115200 8N1 (9600 recommended for RS485).

## Function codes

| FC | Use | Status |
|----|-----|--------|
| 0x03 | Read holding registers | documented; verified working (no auth for public areas) |
| 0x04 | Read input registers | in Dart client, untested |
| 0x06 | Write single register | in Dart client; used in probes |
| 0x10 | Write multiple registers | used by cloud syncTime (RTC) |

Error reply: `FC|0x80` + exception code:

| Code | Meaning |
|------|---------|
| 0x01 | Unsupported function |
| 0x02 | Illegal register address (out of range) — **a block read fails entirely if ANY register in it is invalid** |
| 0x03 | Illegal value |
| 0x04 | Operation failed — returned for the **auth-gated config area (0x0400+)** |
| 0x05 | Length error (>256 B) |
| 0x06 | CRC failed |
| 0x07 | Read-only parameter |
| 0x08 | Wrong password |

## Scaling

Voltage ×0.01 V · Current ×0.1 A · Power ×0.1 W · Energy ×0.1 Wh · Temperature ×0.1 °C (signed).

## Register map

### System Information Area 0x000A–0x0027 (R, ASCII strings) — ✅ verified readable
| Addr | Len | Name | Live value |
|------|-----|------|-----------|
| 0x000A | 1 | Product ID (2=LTB PWM, 3=LTW WiFi PWM, 4=LTM WiFi MPPT, 128=custom) | 4 |
| 0x000B | 6 | Product model | — |
| 0x0011 | 3 | Software release | "1.0.8" |
| 0x0014 | 3 | Hardware version | "1.3.2" |
| 0x0017 | 3 | Protocol version | "1.0.2" |
| 0x001A | 6 | Serial number | "000629252245" |
| 0x0020 | 8 | Manufacturer | "LimuTech" |

### System Configuration Area 0x0050–0x0057 (R) — ✅ verified
max PV V 0x0050 (=80) · max batt V 0x0051 (=36) · max charge A 0x0052 (=30) · max load A 0x0053 (=30) · battery count 0x0054 (=1) · capability bitfields "Configuration Item 1/2/3" 0x0055–0x0057 (=0x351E/0x1DAB/0x0063) (soft power, restart, factory reset, fw upgrade, OTA, BT/RS232/RS485/CAN/USB/WiFi/cellular, auto voltage detect, USB port, fast charge, temp monitoring, calibration, load control, RTC sync, GPS, fan, statistics, standby modes…)

### Connect Data Area 0x0080–0x008B (R) — ✅ verified
| Addr | Name | Live value |
|------|------|-----------|
| 0x0080 | GPS fixed | 0 |
| 0x0081/83 | Longitude / Latitude (2 regs each) | 0 |
| 0x0085 | Altitude | 0 |
| 0x0086 | BT networking (0 none / 1 slave / 2 host) | 0 → later 1 |
| 0x0087 | BT connection (0 none / 1 link / 2 wrong password / 3 app) | 1 → later 3 |
| 0x0088 | Wi-Fi: **high byte = signal (signed dBm), low byte = linkage** | 0xCA01 → −54 dBm, connected |
| 0x0089 | Mobile network (byte0 linkage, byte1 signal) | 0 (no cellular) |
| 0x008A | **MQTT server connection 0/1** | 1 |
| 0x008B | **MQTT subscription 0 failed / 1 ok** | 1 |

### Running Data Area 0x00A0–0x00BB (R) — 0x00A0–0x00AF verified
| Addr | Name | Live value |
|------|------|-----------|
| 0x00A0 | Running state (0 power-on delay, 1 upgrading, 2 upgrade failed, 3 init, 4 battery activated, 5 running, 6 manual shutdown) | 5 |
| 0x00A1 | Fault code (4 regs) | 0 |
| 0x00A5/6/7 | PV voltage / current / power | 21.09 V / 0.3 A / 7.6 W |
| 0x00A8 | Battery type (bits14-15: 1 lead-acid, 2 lithium; bits8-13 ID; bits0-7 string count) | 0x8004 = lithium ×4 |
| 0x00A9 | Battery rated voltage | 1 — no app equivalent; **not published** (undecodable) |
| 0x00AA | Accumulator voltage | 14.00 V |
| 0x00AB | SOC ×0.1 % (manual) — raw 100 read on a full 4S lithium pack ⇒ more likely ×1 %; PoC uses ×1 | 100 → 100 % (assumed) |
| 0x00AC | byte0 charge phase (0 off,1 fast,2 equalize,3 float,4 balance,5 MPPT,6 pause), byte1 charge switch | 0x0105 |
| 0x00AD/AE/AF | Charging voltage / current / power | 20.92 V / 0.1 A / −5.6 W |
| 0x00B0–B3 | Load switch / V / A / W (switch is **writable**, FC10) | 0 / 0 / 0 / 0 (✅ read 2026-08-11) |
| 0x00B4–B7 | USB switch / V / A / W (switch **writable**) | 0 / 0 / 16 / 16 (✅ read 2026-08-11) |
| 0x00B8 | Controller temperature ×0.1 C | 304 ⇒ 30.4 C (✅ read 2026-08-11) |
| 0x00B9 | External temperature (optional sensor) | 0xFFFF = no sensor; **not published** |
| 0x00BA/BB | Fan switch (**writable**) / speed | 0 / 0 |

> Charge current/power (0x00AE/AF) are **signed** 16-bit: a discharge reads a
> negative word, e.g. 0xFFC8 = −56 ⇒ −5.6 W at ×0.1 W. (Live 2026-08-11.) 

> Note: reads must not span segment boundaries. 0x00A0-0x00AF and 0x00B0-0x00BF
> must be read as two separate blocks; a single 0x00A0→0x00BB read fails 0x04.
> Load/USB/fan switches are exposed as writable HA switches; the bridge writes
> FC10 to 0x00B0/0x00B4/0x00BA. Real-write confirmation pending next deploy.

Fault bitmap (0x00A1, 4 B): byte0 = PV reversed, PV overV, charge-circuit open/short, battery absent/reversed/overV/overcharge/underV, low-power disconnect, hi/lo-temp protections, charge overcurrent (1.3×); byte1 = load circuit fault, load overcurrent levels, USB overV/lowV/overcurrent, internal temp states; byte2 = GPS fail, **wrong password**, Wi-Fi lost, cellular lost, server not connected, server warning, time-sync failed; byte3 reserved.

### Event logs (R)
Controller events: count at 0x0100, records at 0x0101… (6 regs × 20). Communication events: 0x0200 + 0x0201…. Record = word0 event code, word1 battery V, words2-3 date, words4-5 h:m:s.

Event codes: 1 power-on ok · 2 init fail · 3/4 start/end charging · 5/6 load on/off · 7/8 charge circuit open/short · 9/10 load open/short · 11 batt low alarm · 12 batt low-V protect · 13 batt overpressure · 14 overheat · 15 load short · 16/17 charge on/off · 18/19 USB on/off · 20 PV overV · 21 charge overcurrent · 22 battery absent · **128 BT wrong password · 129 BT connected · 130/131 Wi-Fi fail/ok · 132/133 MQTT connect fail/ok · 134/135 MQTT subscribe fail/ok**.

### Statistics Area 0x0300–0x0378 (R)
Total runtime (2, s — statically 8 across 5 s, unit unconfirmed) · cumulative generation (2, Wh — measured 0x001C008C=1,835,148 ⇒ 1835.1 kWh; manual's "kWh" label is really Wh) · total consumption (2) · full-charge count · over-discharge count · today's block (0x0308–0x0313): generation Wh, PV max V,A,W / batt max-min V / consumption Wh / load max A,W / USB Wh,max A · yesterday at 0x0314 (same layout, not read by the bridge) … daily blocks; statistical log: 12-register records, date at word 14, h:m:s at words 15–16. The bridge reads 0x0300–0x0313 in one 20-reg block (✅ 2026-08-11).

### Undocumented / gated areas
* **0x0400–0x0480+**: reads → exception 0x04 (operation failed). Almost certainly the **BLE-password-gated config area** (Wi-Fi credentials, MQTT settings, charge parameters). The manual's pages 13-14 (covering 0x0400+) are missing from the PDF.
  * **Unlock (2026-08-10)**: FC 0x10, ASCII PIN packed 2 chars/register, written to **`0x0400`** — e.g. `000000` → regs `[0x3030,0x3030,0x3030]`, frame `FF 10 04 00 00 03 06 30 30 30 30 30 30 20 FB`. The device replies EXC `0x02` to the write but honours it (gated reads then succeed); unlocked state persists across reconnects, resets on power cycle.
* **RTC at 0x0504–0x0507 (RW)** — ✅ verified: `[year][month<<8|day][hour<<8|min][sec<<8|0]`; written by the cloud's syncTime (FC 0x10). Readable WITHOUT password.
* 0x0500–0x0503: unknown; plain ASCII PIN writes there → 0x04 (the documented 0x0400 PIN register is the correct one, see above).

## Reproduction

```bash
TOK="<32-hex token>"   # from POST /api/login/account
curl -s https://lmsolar.wyadmin.com/api/user/info -H "token: $TOK"
# tunnel a read of PV V/A/W (0x00A5..0x00A7), frame FF 03 00A5 0003 + big-endian CRC:
curl -s -X POST https://lmsolar.wyadmin.com/api/hmBridge -H "token: $TOK" \
     -H "Content-Type: application/json" \
     -d '{"node_id":"000629252245","data":[255,3,0,165,0,3,54,0]}'
# NOTE: hmBridge currently 500-crashes server-side (2026-08-02), even with device online.

# local BLE (works today):
python ../bluetooth/ble_modbus.py multi "0x00A0:1,0x00A5:3,0x0088:1,0x0504:4"
```
