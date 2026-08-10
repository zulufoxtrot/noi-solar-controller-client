"""MQTT publisher with Home Assistant MQTT auto-discovery.

State is published as retained per-key topics:
    <prefix>/<node_id>/<key>            e.g. limu_solar/000629252245/pv_power
Availability (LWT):
    <prefix>/<node_id>/availability     "online" / "offline"
Discovery (retained):
    <disc>/sensor/<node_id>/<key>/config
    <disc>/binary_sensor/<node_id>/<key>/config
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable

from paho.mqtt import client as mqtt

try:
    from paho.mqtt.client import CallbackAPIVersion

    _HAVE_CALLBACK_API = True
except ImportError:  # paho-mqtt < 2.0
    CallbackAPIVersion = None
    _HAVE_CALLBACK_API = False

log = logging.getLogger(__name__)

# key -> (component, name, unit, device_class, state_class, icon, entity_category, options)
SENSORS: dict[str, tuple] = {
    "pv_voltage": ("sensor", "PV Voltage", "V", "voltage", "measurement", None, None, None),
    "pv_current": ("sensor", "PV Current", "A", "current", "measurement", None, None, None),
    "pv_power": ("sensor", "PV Power", "W", "power", "measurement", None, None, None),
    "battery_voltage": ("sensor", "Battery Voltage", "V", "voltage", "measurement", None, None, None),
    "battery_soc": ("sensor", "Battery SOC", "%", "battery", "measurement", None, None, None),
    "battery_type": ("sensor", "Battery Type", None, None, None, "mdi:battery", "diagnostic", None),
    "battery_rated_voltage": ("sensor", "Battery Rated Voltage", "V", "voltage", None, None, "diagnostic", None),
    "running_state": ("sensor", "Running State", None, "enum", None, "mdi:solar-power", None,
                      ["power_on_delay", "upgrading", "upgrade_failed", "init",
                       "battery_activated", "running", "manual_shutdown", "unknown"]),
    "fault_code": ("sensor", "Fault Code", None, None, None, "mdi:alert-circle-outline", "diagnostic", None),
    "charge_phase": ("sensor", "Charge Phase", None, "enum", None, "mdi:battery-charging", None,
                     ["off", "fast", "equalize", "float", "balance", "mppt", "pause", "unknown"]),
    "charge_switch": ("sensor", "Charge Switch", None, "enum", None, "mdi:toggle-switch", None,
                      ["on", "off"]),
    "charge_voltage": ("sensor", "Charge Voltage", "V", "voltage", "measurement", None, None, None),
    "charge_current": ("sensor", "Charge Current", "A", "current", "measurement", None, None, None),
    "charge_power": ("sensor", "Charge Power", "W", "power", "measurement", None, None, None),
    "wifi_rssi": ("sensor", "Wi-Fi RSSI", "dBm", "signal_strength", "measurement", None, "diagnostic", None),
    "wifi_connected": ("binary_sensor", "Wi-Fi", None, "connectivity", None, None, "diagnostic", None),
    "cloud_mqtt_connected": ("binary_sensor", "Cloud MQTT", None, "connectivity", None, None, "diagnostic", None),
    "cloud_mqtt_subscribed": ("binary_sensor", "Cloud MQTT Subscription", None, "connectivity", None, None, "diagnostic", None),
}


class MqttBridge:
    def __init__(
        self,
        cfg,
        node_id: str,
        device_info: dict,
        on_pair: Callable[[bool], None] | None = None,
    ):
        self._cfg = cfg
        self.node_id = node_id
        self.base = f"{cfg.mqtt_topic_prefix}/{node_id}"
        self.availability_topic = f"{self.base}/availability"
        self.bridge_availability_topic = f"{self.base}/availability_bridge"
        self.pairing_topic = f"{self.base}/pairing"
        self.pairing_set_topic = f"{self.base}/pairing/set"
        self._on_pair = on_pair
        self._device = {
            "ids": [node_id],
            "name": cfg.device_name,
            "mf": device_info.get("manufacturer") or "LimuTech",
            "mdl": device_info.get("model") or "unknown",
            "sw": device_info.get("sw_version") or "unknown",
            "sn": device_info.get("serial_number") or node_id,
        }
        if _HAVE_CALLBACK_API:
            self._client = mqtt.Client(
                CallbackAPIVersion.VERSION2,
                client_id=cfg.mqtt_client_id,
                clean_session=True,
            )
            self._client.on_connect = self._on_connect_v2
            self._client.on_disconnect = self._on_disconnect_v2
        else:  # paho-mqtt < 2.0
            self._client = mqtt.Client(
                client_id=cfg.mqtt_client_id, clean_session=True
            )
            self._client.on_connect = self._on_connect
            self._client.on_disconnect = self._on_disconnect_v1
        self._client.on_message = self._on_message
        if cfg.mqtt_username:
            self._client.username_pw_set(cfg.mqtt_username, cfg.mqtt_password)
        self._client.will_set(self.availability_topic, "offline", retain=True)

    def _on_connect(self, client, _userdata, _flags, rc):  # paho v1
        if rc == 0:
            log.info("MQTT connected to %s:%s", self._cfg.mqtt_host, self._cfg.mqtt_port)
            client.publish(self.availability_topic, "online", retain=True)
            client.publish(self.bridge_availability_topic, "online", retain=True)
            client.subscribe(self.pairing_set_topic)
        else:
            log.error("MQTT connect refused: %s", rc)

    def _on_connect_v2(self, client, _userdata, _flags, reason_code, _properties):
        if reason_code == 0:
            log.info("MQTT connected to %s:%s", self._cfg.mqtt_host, self._cfg.mqtt_port)
            client.publish(self.availability_topic, "online", retain=True)
            client.publish(self.bridge_availability_topic, "online", retain=True)
            client.subscribe(self.pairing_set_topic)
        else:
            log.error("MQTT connect refused: %s", reason_code)

    def _on_disconnect_v1(self, _client, _userdata, rc):  # paho v1
        if rc != 0:
            log.warning("MQTT disconnected unexpectedly: %s", rc)

    def _on_disconnect_v2(self, _client, _userdata, _flags, reason_code, _properties):
        if reason_code != 0:
            log.warning("MQTT disconnected unexpectedly: %s", reason_code)

    def _on_message(self, _client, _userdata, message) -> None:  # paho thread
        if message.topic != self.pairing_set_topic:
            return
        payload = (message.payload or b"").decode().strip().lower()
        if payload in ("paired", "on", "1", "true", "yes"):
            if self._on_pair is not None:
                self._on_pair(True)
        elif payload in ("unpaired", "off", "0", "false", "no"):
            if self._on_pair is not None:
                self._on_pair(False)
        else:
            log.warning("ignoring unknown pairing command %r", payload)

    def connect(self) -> None:
        self._client.connect(self._cfg.mqtt_host, self._cfg.mqtt_port, keepalive=60)
        self._client.loop_start()

    def close(self) -> None:
        try:
            self._client.publish(self.availability_topic, "offline", retain=True)
            self._client.publish(self.bridge_availability_topic, "offline", retain=True)
        finally:
            self._client.loop_stop()
            self._client.disconnect()

    def publish_discovery(self) -> None:
        """Publish retained HA auto-discovery config for every known sensor."""
        for key, (component, name, unit, dev_cla, state_cla, icon, category, options) in SENSORS.items():
            payload = {
                "name": name,
                "uniq_id": f"{self.node_id}_{key}",
                "stat_t": f"{self.base}/{key}",
                "avty_t": self.availability_topic,
                "dev": self._device,
            }
            if unit:
                payload["unit_of_meas"] = unit
            if dev_cla:
                payload["dev_cla"] = dev_cla
            if state_cla:
                payload["stat_cla"] = state_cla
            if icon:
                payload["icon"] = icon
            if category:
                payload["ent_cat"] = category
            if options:
                payload["ops"] = options
            if component == "binary_sensor":
                payload["pl_on"] = "on"
                payload["pl_off"] = "off"
            topic = f"{self._cfg.mqtt_discovery_prefix}/{component}/{self.node_id}/{key}/config"
            self._client.publish(topic, json.dumps(payload), retain=True)
        switch = {
            "name": "BLE Pairing",
            "uniq_id": f"{self.node_id}_pairing",
            "stat_t": self.pairing_topic,
            "cmd_t": self.pairing_set_topic,
            "pl_on": "paired",
            "pl_off": "unpaired",
            "stat_on": "paired",
            "stat_off": "unpaired",
            "avty_t": self.bridge_availability_topic,
            "dev": self._device,
            "icon": "mdi:bluetooth",
            "ent_cat": "diagnostic",
        }
        switch_topic = (
            f"{self._cfg.mqtt_discovery_prefix}/switch/{self.node_id}/pairing/config"
        )
        self._client.publish(switch_topic, json.dumps(switch), retain=True)
        log.info(
            "published HA discovery for %d entities + pairing switch under %s",
            len(SENSORS),
            self._cfg.mqtt_discovery_prefix,
        )

    def set_availability(self, online: bool) -> None:
        self._client.publish(
            self.availability_topic, "online" if online else "offline", retain=True
        )

    def publish_pairing(self, paired: bool) -> None:
        self._client.publish(
            self.pairing_topic, "paired" if paired else "unpaired", retain=True
        )

    def publish_state(self, values: dict) -> None:
        for key, value in values.items():
            if key not in SENSORS:
                continue
            self._client.publish(f"{self.base}/{key}", value, retain=True)
