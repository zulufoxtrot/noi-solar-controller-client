# HTTP REST API — lmsolar.wyadmin.com

Base: `https://lmsolar.wyadmin.com` + prefix `/api`
Backend: **ThinkPHP V8.0.3** (leaks version + stack pages on exceptions; CORS `Access-Control-Allow-Origin: *`)

## Envelope

```json
{ "code": 1, "show": 0, "msg": "", "data": {} }
```

| code | meaning |
|------|---------|
| 1 | SUCCESS — `data` is the payload |
| 0 | FAIL — `msg` human-readable error (Chinese or English) |
| -1 | LOGIN_FAILURE — token invalid/expired (`"Login timeout, please log in again"`) |
| 2 | OPEN_NEW_PAGE |

`show=1` ⇒ client should toast `msg`. Methods are enforced per endpoint (wrong method → `{"code":0,"msg":"请求方式错误，请使用get请求方式"}`). Nonexistent routes fall through to a ThinkPHP 500 page (not a JSON 404).

## Auth

* Header **`token: <32-hex session token>`** (NOT `Authorization: Bearer` — ignored).
* Informational header `version:` (web console sends `1.9.0`; not validated).
* Login:

```
POST /api/login/account
{ "terminal": "4", "scene": 1, "account": "you@x.com", "password": "..." }   → password login
{ "terminal": "4", "scene": 2, "mobile": "...", "code": "<sms code>" }       → SMS login
```

* `terminal` = client type (`"4"` = web console). Wrong creds → `{"code":0,"msg":"Invalid username or password"}`.
* Response `data`: `{id, sn, nickname, account, email, mobile, avatar, is_demo, protocol_type, token}` — **no MQTT fields** (verified with fresh login 2026-08-02).
* Tokens expire (`code:-1`); re-login works fine. Session token of 2026-08-02 pm: `ba3d8acf48cb19587a7559455227359d`.

## Endpoint map (probed live)

### Auth & user
```
POST /api/login/register          channel + scene/mobile/code/password
POST /api/login/logout
GET|POST /api/login/check         → {"code":1,"msg":"success"}
GET  /api/login/getScanCode?url=  + POST /api/login/scanLogin   (QR login)
GET  /api/code, /api/code/img     captcha (tied to PHPSESSID cookie)
POST /api/sms/sendCode {mobile}
POST /api/email/sendCode {email}
POST /api/user/resetPassword / resetPasswordEmail
POST /api/user/changePassword     (old pw validated; first error 请输入密码)
POST /api/user/bindEmail / bindMobile
POST /api/user/cancel             account deletion
GET  /api/user/info               → {id, sn, account, nickname, avatar, mobile, email, has_password, ...}
GET  /api/user/center
POST /api/user/setInfo            nickname/avatar/sex/desc
GET  /api/user/collection
```

### Devices
```
GET  /api/device/lists            → {lists:[Device], count, ...}
GET  /api/device/detail?sn=       → Device ("Not found" if token bad too)
GET  /api/device/exists?sn=
POST /api/device/register   {sn}  bind ("No device found" if unknown)
POST /api/device/unregister {sn}
POST /api/device/setInfo    {product_sn, sn, name?...}
POST /api/device/setTimezone{product_sn, sn, ...}
GET  /api/device/prefs      {sn, prefs_id, key}
GET  /api/deviceUser/lists  {product_sn, device_sn}   share list ("No user" = not shared)
POST /api/deviceUser/add / remove
```

Live device record (no online-flag field):

```json
{ "id": 2937, "product_id": 2, "product_sn": "4", "node_id": "000629252245",
  "sn": "000629252245", "ble_mac": "B4:C2:E0:E0:50:BC", "wifi_mac": "",
  "name": "Colombis", "model": "LTM2430", "read_interval": 3000,
  "timezone_name": "Europe/Paris", "timezone_offset": 7200000,
  "is_sync_time": 1, "data_mode": 0, "user_id": 1771, "rule_id": 1 }
```

### Preferences (server-whitelisted keys: `card_value_vertical`, `lead_battery_settings`, `theme`, `use_fahrenheit`)
```
GET|POST /api/userPrefs/globalGet  {key}
GET|POST /api/userPrefs/globalSet  {key, value}
GET|POST /api/userPrefs/productGet {product_sn, key}
GET|POST /api/userPrefs/productSet {product_sn, key, value}
```

### Misc
```
GET  /api/firmware/check {product_sn [, version]}   (500 when no fw records — server bug)
POST /api/feedback/add   {content}
GET  /api/index/kb                    (500 — server bug)
GET  /index/policy?type=privacy|service|cancel   (HTML, no /api prefix)
GET  /api/pc/config                   shop config ("网优智电", login_ways ["1","2","4"])
POST /api/upload/image                multipart field "file"
```

## Device command tunnel

### `POST /api/hmBridge`

Tunnels a raw Modbus-RTU frame to a bound device through the cloud.

```json
REQUEST:  { "node_id": "000629252245", "data": [ <frame bytes> ] }
RESPONSE: { "code": 1, "data": [ <response frame bytes> ] }
```

Observed behavior:
* unknown `node_id` + non-empty `data` → `{"code":0,"msg":"No device found"}`
* valid `node_id` + empty/absent `data` → generic `{"code":0,"msg":""}`
* valid `node_id` + non-empty `data` → **HTTP 500 ThinkPHP stack page** (unhandled null connection).
  First seen while the device was offline; **still crashes after the device came MQTT-online (2026-08-02 ~16:35)** → the server-side relay channel is broken independently of device state. Untestable until the vendor fixes it or another transport detail is discovered.

### `POST /api/control/syncTime`

Cloud builds a set-RTC Modbus frame for the device.

```
REQUEST:  { "node_id": "000629252245" }
RESPONSE: { "code": 1, "data": [255, 16, 5, 4, 0, 4, 25, 149] }
```

Decoded: `FF 10 0504 0004 | 19 95` = FC 0x10 (write multiple) ack, register **0x0504**, 4 regs. CRC16-Modbus of the body = 0x1995, sent **high byte first** — matching the device's actual big-endian CRC convention (manual says low-first; device firmware disagrees).

## Web console

`https://lmsolar.wyadmin.com/pc/` — Nuxt 3 SPA (Element Plus), version "1.9.0". Same envelope, same `token`/`version` headers. Useful for endpoint cross-checking via its JS bundle.

## Security notes

1. No request signing/encryption: the 32-hex token over TLS is the only protection; CORS allows any origin.
2. `hmBridge`, `/api/firmware/check?product_sn=4`, `/api/index/kb` leak ThinkPHP stack pages (framework 8.0.3).
3. The cloud tunnel needs only an account token with the device bound — the device-level BLE password does not protect it (when it works).
