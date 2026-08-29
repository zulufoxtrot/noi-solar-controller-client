# noi-solar-controller-client

A python client for the **Noi/Limu LTM** solar charge controller over Bluetooth.

Features:
- Bluetooth auto discovery
- Publishes to an MQTT server
- Home Assistant MQTT auto discovery

![photo](images/photo.jpg)

*Depiction of the controller.*

![img.png](images/img.png)

*Telemetry in Home Assistant.*

## Interfaces

The controller has multiple interfaces:

**RS485**

Simple Modbus over RS485, though the RJ45 port. The modbus spec sheet is in the ``rs485`` folder.

**Bluetooth LE**

Basically a Bluetooth wrapper for the modbus interface. Modbus specs are the same.

The default client is the [Noi Solar](https://play.google.com/store/apps/details?id=com.wyadmin.solar.app&hl=en) app.

**Wifi**

Once connected to wifi, the controller connects to a private HTTP & MQTT server: https://lmsolar.wyadmin.com.

The HTTP API handles account management (user creation, pairing, ...).

The MQTT server handles telemetry. The telemetry is then consumed by the Noi Solar app.

I managed to reverse engineer parts of the API, but I failed to connect to the MQTT server.

## Quick start

```bash
cd bluetooth
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# no hardware — mock telemetry sent to MQTT:
SIMULATE=1 MQTT_HOST=192.168.1.10 .venv/bin/python -m app

# with your controller:
MQTT_HOST=192.168.1.10 .venv/bin/python -m app
```

Or run in Docker (needs a Linux host with Bluetooth):

```bash
cd bluetooth && docker compose up -d --build
```

## Keywords

- Limu Solar: Device manufacturer
- Wyadmin / NOI Technologies: Telemetry system / app editor
- NOI Solar: iOS/Android client