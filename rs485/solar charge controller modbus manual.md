# Solar Controller Communication Protocol V1.0.0

Transcription of `solar charge controller modbus manual.pdf` (scanned, 9 pages).
Original title: 太阳能控制器通信协议 V1.0.0 — @力牧科技 (Limu Technology) 2026.

Version Revision History: V1.0.0 — Leo — 202603.

---

## Serial Port Parameters

Default baud rate: 115200, 8-bit data bits, 1 stop bit, no parity.
Note: When using RS485 communication, a baud rate of 9600 is recommended for long lines.

## Format Description

Each register is 2 bytes in size, with its address and value organized in the
high-byte-first, low-byte-last format (big-endian).

A 32-bit number is stored with the high byte occupying the lower register address
and the low byte the higher address. Example: 0x12345678 → address 0x01 = 0x1234,
address 0x02 = 0x5678.

## Reference Format

| Device Address | FC | Data | CRC verification |
|---|---|---|---|
| 1 byte | 1 byte | N byte | 2 byte |

1. **Device Address** — Range: 0x01–0xF7; 0xF8–0xFF reserved.
   0x00 is a broadcast address: the slave receives commands but does not return data.
   0xFF is an all-purpose address: the slave receives commands and returns data,
   applicable only when a single slave is connected.
2. **FC** — 0x03: Read registers (single and multiple). *(Only FC 0x03 is
  documented in this PDF.)*
3. **Data** — Different function codes have different data structures.
4. **Verification** — Standard CRC verification algorithm: all data from the slave
   address up to before the CRC check.
   **Note: Unlike data, the low byte of the CRC portion comes first, followed by
   the high byte** (i.e. standard Modbus little-endian CRC).
   ⚠️ *On-device verification contradicts this — the firmware stores the CRC
   big-endian. See PROTOCOL.md.*

The total length of a Modbus serial communication data unit is 256 bytes, with the
address code, function code, and CRC check sum totaling 4 bytes, leaving up to 252
bytes for the actual data.

## Read Register

Request:

| Device Address | FC | Data | CRC verification |
|---|---|---|---|
| 1 byte | 1 byte | 4 byte | 2 byte |
| | 0x03 | 2 Byte, starting address | 2 Byte; Number of registers: 0x01–0x7d | |

Normal response:

| Device Address | FC | Data | CRC verification |
|---|---|---|---|
| 1 byte | 1 byte | 2\*N+1 bytes | 2 byte |
| | 0x03 | 1 Byte: byte length of the value | 2\*N bytes, register value from 1 to N | |

Error Return:

| Device Address | FC | Data | CRC verification |
|---|---|---|---|
| 1 byte | 1 byte | 1 byte | 2 byte |
| | 0x83 | See the error code table for details | |

## Error Code Table

| Code | Name | Meaning |
|---|---|---|
| 0x01 | Unsupported function code | The device does not support this command |
| 0x02 | Illegal register address | The requested register exceeds the range |
| 0x03 | Illegal register value | The value exceeds the allowed range |
| 0x04 | Operation failed | |
| 0x05 | Length error | Frame length exceeds 256 bytes |
| 0x06 | Validation failed | CRC verification failed |
| 0x07 | Read-only parameter | |
| 0x08 | Wrong password | |

## Data Specification

| Name | Unit | Multiplying power | Explain |
|---|---|---|---|
| Voltage | V | 0.01 | 16-bit unsigned integer 0–65535 → 0 V to 6553.5 V |
| Current | A | 0.1 | 16-bit unsigned integer 0–65535 → 0 A to 6553.5 A |
| Power | W | 0.1 | 16-bit unsigned integer 0–65535 → 0 W to 6553.5 W? |
| Quantity of electricity | Wh | 0.1 | |
| Temperature | °C | 0.1 | 16-bit signed integer −32767 to 32767 → −3276.7 °C to 3276.7 °C |

## Register Address Table

### System Information Area — range 0x000A–0x0027

| Address | Length | Name | RW | Form | Explain |
|---|---|---|---|---|---|
| 0x000A | 1 | Product ID | R | %s | |
| 0x000B | 6 | Product model | R | %s | |
| 0x0011 | 3 | Software release | R | %s | X.X.XX |
| 0x0014 | 3 | Hardware Version | R | %s | X.X.XX |
| 0x0017 | 3 | Protocol Version | R | %s | |
| 0x001A | 6 | Serial number | R | %s | |
| 0x0020 | 8 | Manufacturer | R | %s | |

Reserved alternative address range: 0x0028–0x004F

Product ID table:

| ID | Name | Explain |
|---|---|---|
| 2 | LTB-24xx | BLE PWM |
| 3 | LTW-24xx | Wi-Fi+BLE PWM |
| 4 | LTM-24xx | Wi-Fi+BLE MPPT |
| 128 | | Custom Development Product |

### System Configuration Area — range 0x0050–0x0057

| Address | Length | Name | RW |
|---|---|---|---|
| 0x0050 | 1 | Maximum voltage of photovoltaic panels | R |
| 0x0051 | 1 | Maximum Voltage of the Battery | R |
| 0x0052 | 1 | Maximum Charging Current | R |
| 0x0053 | 1 | Maximum Current Load | R |
| 0x0054 | 1 | Number of Batteries | R |
| 0x0055 | 1 | Configuration Item 1 | R |
| 0x0056 | 1 | Configuration Item 2 | R |
| 0x0057 | 1 | Configuration Item 3 | R |

Reserved alternative address range: 0x0057–0x007F

Configuration Item 1:
- B0: Supports soft power on/off; B1: Supports soft restart; B2: Supports
  restoring factory settings; B3: Supports firmware upgrade; B4: Supports OTA upgrade.
- B8: Supports Bluetooth; B9: Supports RS232; B10: Supports RS485; B11: Supports
  CAN bus; B12: Supports USB; B13: Supports Wi-Fi; B14: Supports mobile networks.

Configuration Item 2:
- B0: Supports automatic voltage detection; B1: Supports USB charging port;
  B2: Supports USB fast charging; B3: Supports internal temperature monitoring;
  B4: Supports external temperature monitoring; B5: Supports temperature
  calibration; B6: Supports temperature compensation; B7: Supports load control;
  B8: Supports load short-circuit protection switch; B9: Supports load power
  adjustment; B10: Supports voltage calibration; B11: Supports charging current
  calibration; B12: Supports discharge current calibration; B13: Supports RTC time
  synchronization; B14: Supports GPS functionality; B15: Supports cooling fan
  operation; B16: Supports cooling fan parameter adjustment.

Configuration Item 3:
- B0: Supports power generation statistics; B1: Supports power consumption
  statistics; B2: Supports USB power consumption statistics; B3: Supports
  battery-free applications; B4: Supports mains power supplementation; B5: Supports
  charging switch control; B6: Supports USB switch control; B7–15: Standby modes.

### Connect Data Area — range 0x0080–0x008B

| Address | Length | Name | RW | Explain |
|---|---|---|---|---|
| 0x0080 | 1 | GPS fixed position | R | |
| 0x0081 | 2 | Longitude | R | |
| 0x0083 | 2 | Latitude | R | |
| 0x0085 | 1 | Altitude | R | |
| 0x0086 | 1 | Bluetooth Networking Status | R | 0: Not Networked; 1: Slave Device; 2: Host |
| 0x0087 | 1 | Bluetooth connection | R | 0: No connection; 1: Connected (Link); 2: Incorrect password; 3: Connected (App) |
| 0x0088 | 1 | WIFI linkage | R | Byte0: linkage; Byte 1: Signal strength |
| 0x0089 | 1 | Mobile Network Connection | R | Byte0: linkage; Byte 1: Signal strength |
| 0x008A | 1 | Server Connection | R | 0: Disconnect; 1: Connect |
| 0x008B | 1 | Subscription Status | R | 0: Failed; 1: Successful |

Reserved alternative address range: 0x008C–0x009F

### Running Data Area — range 0x00A0–0x00BB

| Address | Length | Name | RW | Multiplying power | Unit | Explain |
|---|---|---|---|---|---|---|
| 0x00A0 | 1 | Running state | R | | | See Operation status below |
| 0x00A1 | 4 | Fault code | R | | | See Fault Code below |
| 0x00A5 | 1 | Solar panel voltage | R | 0.01 | V | %d |
| 0x00A6 | 1 | Solar panel current | R | 0.1 | | |
| 0x00A7 | 1 | Solar Panel Power | R | 0.1 | | |
| 0x00A8 | 1 | Battery Type | R | | | Byte14–15: 00 No battery, 01 Lead-acid, 10 Lithium batteries, 11 Reserve; Bytes 8–13: ID; Bytes 0–7: String Count |
| 0x00A9 | 1 | Battery rated voltage | R | | | %d |
| 0x00AA | 1 | Accumulator voltage | R | 0.01 | | %d |
| 0x00AB | 1 | Battery Capacity | R | 0.1 | | %d — Percentage |
| 0x00AC | 1 | Byte0: Charging Phase; Byte1: Charges switch | R | | | Charging stage: 0 Charge Off, 1 Fast Charge, 2 Equal Charge, 3 Floating Charge, 4 Balance, 5 MPPT, 6 Pause/Stop. Charges switch: 0 Off, 1 Open |
| 0x00AD | 1 | Charging voltage | R | 0.01 | | |
| 0x00AE | 1 | Charging current | R | 0.1 | | |
| 0x00AF | 1 | Charging Power | R | 0.1 | | |
| 0x00B0 | 1 | Load switch | R | | | 0: Off, 1: On |
| 0x00B1 | 1 | Load voltage | R | 0.01 | | |
| 0x00B2 | 1 | Load current | R | 0.1 | | |
| 0x00B3 | 1 | Bearing power | R | 0.1 | | |
| 0x00B4 | 1 | USB switch | R | | | 0: Off, 1: On |
| 0x00B5 | 1 | USB voltage | R | 0.01 | | |
| 0x00B6 | 1 | USB Current | R | | | |
| 0x00B7 | 1 | USB power | R | | | |
| 0x00B8 | 1 | Internal Temperature | R | 0.1 | | |
| 0x00B9 | 1 | External temperature | R | 0.1 | | |
| 0x00BA | 1 | Fan Switch | R | | | |
| 0x00BB | 1 | Fan Speed | R | | | |

Reserved alternative address range: 0x00BC–0x00FF

Operation status: 0 – Power-on delay; 1 – Upgrading in progress; 2 – Upgrade
failed; 3 – Initialization; 4 – Battery activated; 5 – Running; 6 – Manual
shutdown. *(Note: the PDF's "0 – FMP fault" line appears to be a typo.)*

### Fault Code (0x00A1, 4 registers)

ErrorCode0 Byte0:
- B0: Photovoltaic panels connected in reverse
- B1: Photovoltaic panel overvoltage protection (when exceeding the controller's
  maximum input voltage for the photovoltaic panel)
- B2~B3: Charging circuit fault: 0 – Normal; 1 – Open circuit; 2 – Short circuit
- B4: Battery not connected
- B5: Battery reversed polarity
- B6: Battery overvoltage
- B7: Battery Overcharge Protection
- B8: Battery under-voltage alarm
- B9: Battery low-power disconnection
- B10: Protection for rechargeable batteries during high-temperature charging
- B11: Low-temperature charging protection for batteries
- B12: High-temperature discharge protection for batteries
- B13: Battery Low-Temperature Discharge Protection
- B14: Overcurrent protection during charging (when current exceeds 1.3 times the
  maximum charging current)
- B15: Retain

ErrorCode1 Byte1:
- B0~B1: Load discharge circuit fault: 0 – Normal; 1 – Open circuit; 2 – Short circuit
- B2–B3: Load protection status: 0 – Normal; 1 – 1.1× Overcurrent; 2 – 1.3×
  Overcurrent; 3 – Load off
- B4–B5: USB fault: 0 – Normal; 1 – Overvoltage; 2 – Low voltage; 3 – Overcurrent
- B6~B7: 0 – Normal; 1 – Internal high temperature; 2 – High-temperature current
  limiting; 3 – High-temperature protection

ErrorCode2 Byte2:
- B0: GPS cannot determine location
- B1: Wrong password
- B2: WIFI connection lost
- B3: Mobile network connection lost
- B4: Server not connected
- B5: Server warning flag
- B6: Time synchronization failed
- B7: Keep

ErrorCode3 Byte3: Keep

### Event Log

Controller Event — range 0x0100–0x0177 (reserved 0x0178–0x01FF):

| Address | Length | Name | RW |
|---|---|---|---|
| 0x0100 | 1 | Total Number of Logs | R |
| 0x0101 ~ 20\*6 | 6 | Log data: one record uses 6 registers. Word 0: Event Code; Word 1: Battery Voltage; Words 2–3: Date; Character 4–5: Hour, Minute, Second | R |

Communication Event — range 0x0200–0x0277 (reserved 0x0278–0x02FF):

| Address | Length | Name | RW |
|---|---|---|---|
| 0x0200 | 1 | Total Number of Logs | R |
| 0x0201 ~ 20\*6 | 6 | Log data: one record uses 6 registers (same layout as Controller Event) | R |

Event Coding:

| Code | Meaning |
|---|---|
| 0 | Invalid Data |
| 1 | Power on the device (initialization complete) |
| 2 | Device powered on (initialization failed). Note: Communication module connection failed. |
| 3 | Start Charging |
| 4 | End Charging |
| 5 | Enable Load |
| 6 | Unload |
| 7 | The charging circuit is open. |
| 8 | Short circuit in the charging circuit |
| 9 | Load circuit open |
| 10 | Load circuit short circuit |
| 11 | Battery low charge alarm |
| 12 | Battery low-voltage protection |
| 13 | Battery overpressure |
| 14 | Internal Overheat Protection |
| 15 | Load short-circuit protection |
| 16 | Enable charging |
| 17 | Turn off charging |
| 18 | Enable USB Output |
| 19 | Turn off USB Output |
| 20 | Photovoltaic panel overvoltage protection |
| 21 | Overcurrent Protection During Charging |
| 22 | The battery is not connected. |
| 20…127 | …… |
| 128 | Incorrect Bluetooth connection password |
| 129 | Bluetooth connection successful |
| 130 | WIFI connection failed: Connection timed out or busy, no response |
| 131 | WIFI connection successful |
| 132 | MQTT connection failed: Connection timed out or busy with no response |
| 133 | MQTT connection successful |
| 134 | MQTT subscription failed: Connection timed out or busy with no response |
| 135 | MQTT subscription successful |
| 136 | …… |

### Statistics Area — range 0x0300–0x0378

| Address | Length | Name | RW | Multiplying power | Unit |
|---|---|---|---|---|---|
| 0x0300 | 2 | Total Running Time | R | | Second |
| 0x0302 | 2 | Cumulative Power Generation | R | 0.1 | KWh |
| 0x0304 | 2 | Total Electricity Consumption | R | 0.1 | KWh |
| 0x0306 | 1 | Total number of full charges for the battery | R | | |
| 0x0307 | 1 | Total number of over-discharge cycles for the battery | R | | |
| 0x0308 | 1 | Electricity generated on that day | R | 0.1 | Wh |
| 0x0309 | 1 | The maximum voltage of the photovoltaic panels on that day | R | 0.01 | V |
| 0x030A | 1 | The maximum current of the photovoltaic panels on that day | R | 0.1 | A |
| 0x030B | 1 | The maximum power of the photovoltaic panels on that day | R | 0.1 | W |
| 0x030C | 1 | The maximum voltage of the battery on that day | R | 0.01 | V |
| 0x030D | 1 | The lowest battery voltage on that day | R | 0.01 | V |
| 0x030E | 1 | Electricity consumption on that day | R | 0.1 | Wh |
| 0x030F | 1 | Maximum Current at Load Limit on That Day | R | 0.1 | A |
| 0x0310 | 1 | Maximum power load on that day | R | 0.1 | W |
| 0x0311 | 1 | USB power consumption on that day | R | 0.1 | Wh |
| 0x0312 | 1 | Maximum USB current on that day | R | 0.1 | A |
| 0x0313 | 1 | Maximum USB power on that day | R | 0.1 | W |
| 0x0314 | 1 | Electricity generated yesterday | R | 0.1 | Wh |
| …… | 1 | The maximum voltage of the photovoltaic panels yesterday | R | 0.01 | V |

*(daily blocks repeat — "yesterday" at 0x0314 uses the same 12-register layout as
0x0308–0x0313)*

Reserved alternative address range: 0x0379–0x04FF

### Statistical Log

| Address | Length | Name | RW |
|---|---|---|---|
| 0x0300 ~ 7\*16 | 1 | Log data: one record uses 12 registers. See the daily data in the table above, pages 13–14: Date; Words 15–16: Hours, Minutes, Seconds | R |

---

## Notes about this PDF

* The PDF has 9 pages and ends at the Statistical Log. The "pages 13–14" it
  references are **not included** — anything at 0x0400+ (config/auth area) is
  undocumented here.
* Only FC 0x03 is documented; FC 0x06/0x10 exist on-device but are not in this PDF.
* The load/USB/fan "switch" registers (0x00B0/0x00B4/0x00BA) are documented as
  **R** (read-only) here.
