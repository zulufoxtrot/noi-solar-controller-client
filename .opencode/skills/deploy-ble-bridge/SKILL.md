---
name: deploy-ble-bridge
description: Deploy or update the noi-solar-controller-client BLE→MQTT bridge on the production Orange Pi SBC (youri@10.6.0.3, hostname "colombis"). Use when the user asks to deploy, update, test, or roll back the bridge, or to run the bridge image on the SBC. Covers the GHCR build-and-pull flow via the main branch, the local-build flow, verification, and the mandatory cleanup + restore of the production container.
---

# Deploying the BLE→MQTT bridge to the SBC

Production target: `youri@10.6.0.3` (Orange Pi Zero 3, hostname `colombis`,
Allwinner sunxi64, UWE5622 Bluetooth via `hciattach_opi`). The bridge runs in a
Docker container managed by a Portainer "portable-feeder" stack, image pinned
by commit SHA (`ghcr.io/zulufoxtrot/noi-solar-controller-client:<sha>`), container
name `limu-solar`. (Renamed from the old Portainer-style
`377b5212aa78_limu-solar` around 2026-08-20 — the old name appears in stale
docs and log excerpts.)

## Golden rules

1. **Never leave the production container stopped.** Whatever you test,
   `limu-solar` must be running and healthy when you're done.
2. **Never run two instances of the bridge at once.** The controller is
   single-link: it stops advertising while connected. A second bridge would
   steal the link and both would flap. Stop the production container before
   starting a test one.
3. Docker on the SBC needs `sudo` (NOPASSWD is set up for youri via
   `/etc/sudoers.d/99-opencode-temp` during a session; use `sudo docker ...`).
4. SSH is non-interactive: use `ssh -o BatchMode=yes youri@10.6.0.3 '...'`.

## Flow 1: GHCR build via the main branch (preferred for real deploys)

The repo CI (`.github/workflows/ghcr.yml`, `on: [push]` — any branch) builds
and pushes on every push, tagging both `latest` and `:<sha>`. Deploying a code
change:

1. Commit locally on `main`, then push:
   ```bash
   git push origin main
   ```
   (Historical note: before 2026-08-22 deploys went via a `refactor` branch;
   that branch's Aug-18/19 lineage shipped a broken build missing the GATT
   `start_notify` call — telemetry timed out on every read. `main` is now the
   deploy source of truth.)
2. Watch the build:
   ```bash
   gh run list --limit 1
   gh run watch   # or gh run view <id> --log-failed
   ```
3. Pull the new image on the SBC (by SHA to avoid ambiguity):
   ```bash
   ssh -o BatchMode=yes youri@10.6.0.3 'sudo docker pull ghcr.io/zulufoxtrot/noi-solar-controller-client:<sha>'
   ```
4. Swap the running container. It's a Portainer stack (no local compose file),
   so recreate with the same env/mounts/network rather than editing a compose
   file. The verified recipe (replace `<SHA>`):
   ```bash
   # read current config first, preserve exactly:
   ssh ... 'sudo docker inspect limu-solar --format "{{json .Config.Env}}\n{{.HostConfig.NetworkMode}} {{.HostConfig.RestartPolicy.Name}}\n{{json .HostConfig.Binds}}"'
   # then swap:
   ssh ... 'sudo docker stop limu-solar && sudo docker rm limu-solar && \
     sudo docker run -d --name limu-solar --network host --restart unless-stopped \
       -v /var/run/dbus:/var/run/dbus:ro \
       -e MQTT_CLIENT_ID=noi-solar-colombis -e SIMULATE=false \
       -e CONTROLLER_ADDRESS=B4:C2:E0:E0:50:BC -e MQTT_PORT=1883 -e MQTT_USERNAME=mqtt \
       -e POLL_INTERVAL=30 -e BLE_ADAPTER=hci0 -e TZ=Europe/Paris -e LOG_LEVEL=INFO \
       -e CONTROLLER_NAME_PREFIX=LTM- -e BLE_PIN=000000 -e MQTT_TOPIC_PREFIX=noi_solar \
       -e MQTT_HOST=10.0.0.2 -e "DEVICE_NAME=Limu Solar Controller (Colombis)" \
       -e MQTT_DISCOVERY_PREFIX=homeassistant -e MQTT_PASSWORD=mqtt \
       ghcr.io/zulufoxtrot/noi-solar-controller-client:<SHA>'
   ```
   The env in the repo compose is NOT what the SBC runs — the Portainer stack
   overrides it (`MQTT_HOST=10.0.0.2`, `MQTT_USERNAME/PASSWORD=mqtt`,
   `MQTT_TOPIC_PREFIX=noi_solar`, `DEVICE_NAME=... (Colombis)`, `BLE_PIN=000000`).

## Flow 2: local build/test on the SBC (for quick experiments)

1. Copy the code up and build a test image:
   ```bash
   scp -r bluetooth youri@10.6.0.3:/tmp/ble-bridge-test
   ssh ... 'sudo docker build -t limu-test /tmp/ble-bridge-test/bluetooth'
   ```
2. Stop the production container, start the test one with the SAME env the
   production one has (read it with `docker inspect` first; it's not in the
   repo compose — the SBC stack overrides `MQTT_HOST=10.0.0.2`,
   `CONTROLLER_ADDRESS=B4:C2:E0:E0:50:BC`, `BLE_ADAPTER=hci0`,
   `MQTT_TOPIC_PREFIX=noi_solar`):
   ```bash
   ssh ... 'sudo docker stop limu-solar && sudo docker run -d --rm --network host \
     --name limu-test -v /var/run/dbus:/var/run/dbus:ro -e <same env> limu-test'
   ```
   The container needs `/var/run/dbus` mounted ro to reach host BlueZ.
3. Verify (see below), then clean up and restore:
   ```bash
   ssh ... 'sudo docker rm -f limu-test; sudo docker rmi limu-test; \
     sudo docker start limu-solar; rm -rf /tmp/ble-bridge-test'
   ```

## Verifying the bridge

```bash
ssh -o BatchMode=yes youri@10.6.0.3 'sudo docker logs limu-solar --tail 20'
```

Healthy log signature:
```
app.ble_client: selected 'LTM-252245' at B4:C2:E0:E0:50:BC (RSSI ...)
app.ble_client: connected to ...
app.main: PV ... V / ... W | batt ... V / ...% | charge ...
```

Also confirm on the host (the radio must be attached):
```bash
ssh ... 'systemctl is-active bluetooth; hciconfig hci0; systemctl status aw859a-bluetooth.service --no-pager -l'
```

## Controller fw 2.0.4 BLE behavior (learned 2026-08-22/24)

* **Session opener rule (critical)**: the FIRST Modbus frame of every BLE
  connection must be a sysinfo-area read (the vendor app always does this).
  Skipping it makes the controller gate ALL traffic with EXC `0x04` until a
  future session opens correctly. Never skip the opener, whatever caching
  optimization suggests otherwise.
* Links die within ~90–100 s of connect regardless of traffic → production
  runs **burst mode** (`BURST_MODE=1`, `POST_CONNECT_SECONDS=0`,
  `READ_TIMEOUT=4`, `ROTATE_GAP_SECONDS=300`, `RETRY_INTERVAL=600`): connect,
  opener, one sample, release, repeat every ~5 min. Extended blocks
  (extension/stats/connect/fault) are polled as optional all-or-nothing
  groups appended AFTER the mandatory vendor-cadence prefix in
  `RUNNING_SMALL_READS` — never reorder or shrink the prefix.
* A PIN write in a *correctly-opened* session behaves like old firmware
  (EXC `0x02` reject-but-honor, link survives). In a *poll-first* session it
  drops the link in ~2 s. Keep `BLE_PIN=000000` set; the lazy-unlock backstop
  works.
* Advert payload carries Limu manufacturer data (currently constant `W1`);
  service `00112233-4455-6677-8899-aabbccddeeff` (notify `00112333`, write
  `00112433`) exists parallel to the Modbus tunnel — purpose unknown, probe
  script staged at `/tmp/gatt_probe.py` on colombis.
* If the controller gets power-cycled or its firmware changes, revisit these
  knobs — healthy firmware may allow sustained sessions again
  (`BURST_MODE=0`).

## Host-side BlueZ breakage (the 2026-08-14 incident)

An `apt-get upgrade` of bluez restarted `bluetooth.service`; on this board the
HCI attach (`hciattach_opi` via `aw859a-bluetooth.service`) failed at boot, so
`bluetoothd` ran with **no radio**. Symptom in the bridge logs:
`Failed to activate service 'org.bluez': timed out`, then `adapter 'hci0' not
found`. Fix (already applied to the SBC, kept for reference):
`sudo systemctl restart aw859a-bluetooth.service`, plus a drop-in so it retries:
`/etc/systemd/system/aw859a-bluetooth.service.d/override.conf` with
`Restart=on-failure` / `RestartSec=2`. See `bluetooth/README.md` →
"Troubleshooting".
