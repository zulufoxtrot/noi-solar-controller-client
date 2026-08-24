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

# keys that map to writable registers; HA publishes to <base>/<key>/set and the
# bridge forwards the on/off value to main.py's on_command(client writer).
SWITCH_KEYS = ("load_switch", "usb_switch", "fan_switch")

# key -> (component, name, unit, device_class, state_class, icon, entity_category, options)
SENSORS: dict[str, tuple] = {
    "pv_voltage": ("sensor", "PV Voltage", "V", "voltage", "measurement", None, None, None),
    "pv_current": ("sensor", "PV Current", "A", "current", "measurement", None, None, None),
    "pv_power": ("sensor", "PV Power", "W", "power", "measurement", None, None, None),
    "battery_voltage": ("sensor", "Battery Voltage", "V", "voltage", "measurement", None, None, None),
    "battery_soc": ("sensor", "Battery", "%", "battery", "measurement", None, None, None),
    "battery_type": ("sensor", "Battery Type", None, None, None, "mdi:battery", "diagnostic", None),
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
    # load / USB outlets, controller temp, fan (0x00B0 block); the three
    # switches are writable `switch` entities driven by main.py's on_command.
    "load_switch": ("switch", "Load Switch", None, None, None, "mdi:power-socket-eu", None, None),
    "load_voltage": ("sensor", "Load Voltage", "V", "voltage", "measurement", None, None, None),
    "load_current": ("sensor", "Load Current", "A", "current", "measurement", None, None, None),
    "load_power": ("sensor", "Load Power", "W", "power", "measurement", None, None, None),
    "usb_switch": ("switch", "USB Switch", None, None, None, "mdi:usb", None, None),
    "usb_voltage": ("sensor", "USB Voltage", "V", "voltage", "measurement", None, None, None),
    "usb_current": ("sensor", "USB Current", "A", "current", "measurement", None, None, None),
    "usb_power": ("sensor", "USB Power", "W", "power", "measurement", None, None, None),
    "controller_temp_c": ("sensor", "Controller Temperature", "°C", "temperature", "measurement", "mdi:thermometer", None, None),
    "ble_pairing_state": ("sensor", "BLE Pairing State", None, "connectivity", None, "mdi:bluetooth", "diagnostic",
                          ["connecting", "connected", "unpaired", "disconnected"]),
    "battery_rated_voltage": ("sensor", "Battery Rated Voltage", "V", "voltage", None, "mdi:battery", "diagnostic", None),
    "fan_switch": ("switch", "Fan Switch", None, None, None, "mdi:fan", None, None),
    "fan_speed": ("sensor", "Fan Speed", None, "speed", "measurement", "mdi:fan", None, None),
    # lifetime / today statistics (0x0300 block)
    "total_runtime_s": ("sensor", "Total Runtime", "s", "duration", "total_increasing", "mdi:timer-sand", "diagnostic", None),
    "total_generation_kwh": ("sensor", "Total Generation", "kWh", "energy", "total_increasing", "mdi:solar-power", "diagnostic", None),
    "total_consumption_kwh": ("sensor", "Total Consumption", "kWh", "energy", "total_increasing", "mdi:power-plug", "diagnostic", None),
    "full_charge_count": ("sensor", "Full Charge Count", None, None, "total_increasing", "mdi:battery-charging", "diagnostic", None),
    "over_discharge_count": ("sensor", "Over-Discharge Count", None, None, "total_increasing", "mdi:battery-alert", "diagnostic", None),
    "today_generation_kwh": ("sensor", "Today's Generation", "kWh", "energy", "total_increasing", "mdi:solar-power", "diagnostic", None),
    "today_max_pv_v": ("sensor", "Today Max PV Voltage", "V", "voltage", "measurement", None, "diagnostic", None),
    "today_max_pv_a": ("sensor", "Today Max PV Current", "A", "current", "measurement", None, "diagnostic", None),
    "today_max_pv_w": ("sensor", "Today Max PV Power", "W", "power", "measurement", None, "diagnostic", None),
    "today_max_batt_v": ("sensor", "Today Max Battery Voltage", "V", "voltage", "measurement", None, "diagnostic", None),
    "today_min_batt_v": ("sensor", "Today Min Battery Voltage", "V", "voltage", "measurement", None, "diagnostic", None),
    "today_consumption_kwh": ("sensor", "Today's Consumption", "kWh", "energy", "total_increasing", "mdi:power-plug", "diagnostic", None),
    "today_max_load_a": ("sensor", "Today Max Load Current", "A", "current", "measurement", None, "diagnostic", None),
    "today_max_load_w": ("sensor", "Today Max Load Power", "W", "power", "measurement", None, "diagnostic", None),
    "today_usb_consumption_kwh": ("sensor", "Today USB Consumption", "kWh", "energy", "total_increasing", "mdi:usb", "diagnostic", None),
    "today_max_usb_a": ("sensor", "Today Max USB Current", "A", "current", "measurement", None, "diagnostic", None),
    "today_max_usb_w": ("sensor", "Today Max USB Power", "W", "power", "measurement", None, "diagnostic", None),
}

# state key -> attribute name published on the battery entity's json_attr_t
# topic. Kept as a mapping so the attribute keys can differ from the MQTT keys.
BATTERY_ATTRIBUTES: dict[str, str] = {
    "battery_soc": "battery_soc",
    "battery_voltage": "battery_voltage",
    "charge_power": "battery_charge_power",
    "charge_voltage": "battery_charge_voltage",
    "charge_current": "battery_charge_current",
}

BATTERY_ENTITY_KEY = "battery_soc"  # entity carrying the battery json attributes

# Generous physical bounds (any LTM voltage/current config must fit); they only
# need to catch impossible frames, e.g. ASCII bytes decoded as register words.
PLAUSIBLE_RANGES: dict[str, tuple[float, float]] = {
    "pv_voltage": (0.0, 250.0),
    "pv_power": (-5.0, 5000.0),
    "battery_voltage": (0.5, 120.0),
    "battery_soc": (0.0, 100.0),
    "charge_power": (-500.0, 5000.0),
}


class MqttBridge:
    def __init__(
        self,
        cfg,
        node_id: str,
        device_info: dict,
        on_pair: Callable[[bool], None] | None = None,
        on_command: Callable[[str, bool], None] | None = None,
    ):
        self._cfg = cfg
        self.node_id = node_id
        self.base = f"{cfg.mqtt_topic_prefix}/{node_id}"
        self.availability_topic = f"{self.base}/availability"
        self.bridge_availability_topic = f"{self.base}/availability_bridge"
        self.pairing_topic = f"{self.base}/pairing"
        self.pairing_set_topic = f"{self.base}/pairing/set"
        self._on_pair = on_pair
        self._on_command = on_command
        self._cmd_topics = {f"{self.base}/{k}/set": k for k in SWITCH_KEYS}
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
            for topic in self._cmd_topics:
                client.subscribe(topic)
        else:
            log.error("MQTT connect refused: %s", rc)

    def _on_connect_v2(self, client, _userdata, _flags, reason_code, _properties):
        if reason_code == 0:
            log.info("MQTT connected to %s:%s", self._cfg.mqtt_host, self._cfg.mqtt_port)
            client.publish(self.availability_topic, "online", retain=True)
            client.publish(self.bridge_availability_topic, "online", retain=True)
            client.subscribe(self.pairing_set_topic)
            for topic in self._cmd_topics:
                client.subscribe(topic)
        else:
            log.error("MQTT connect refused: %s", reason_code)

    def _on_disconnect_v1(self, _client, _userdata, rc):  # paho v1
        if rc != 0:
            log.warning("MQTT disconnected unexpectedly: %s", rc)

    def _on_disconnect_v2(self, _client, _userdata, _flags, reason_code, _properties):
        if reason_code != 0:
            log.warning("MQTT disconnected unexpectedly: %s", reason_code)

    def _on_message(self, _client, _userdata, message) -> None:  # paho thread
        topic = message.topic
        payload = (message.payload or b"").decode().strip().lower()
        if topic == self.pairing_set_topic:
            log.info("MQTT command on %s: %r", topic, payload)
            if payload in ("paired", "on", "1", "true", "yes"):
                if self._on_pair is not None:
                    self._on_pair(True)
            elif payload in ("unpaired", "off", "0", "false", "no"):
                if self._on_pair is not None:
                    self._on_pair(False)
            else:
                log.warning("ignoring unknown pairing command %r", payload)
            return
        key = self._cmd_topics.get(topic)
        if key is not None:
            log.info("MQTT command on %s: %r", topic, payload)
            if payload in ("on", "1", "true", "yes"):
                if self._on_command is not None:
                    self._on_command(key, True)
            elif payload in ("off", "0", "false", "no"):
                if self._on_command is not None:
                    self._on_command(key, False)
            else:
                log.warning("ignoring unknown switch command %r for %s", payload, key)
            return
        log.warning("ignoring message on unsubscribed topic %r", topic)

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
        # Ghost-entity cleanup: an old code version once published an
        # external-temp entity (register 0xB9 reads 0xFFFF on units without
        # the sensor). Empty retained payloads remove its discovery and state
        # topics from the broker/HA.
        for topic in (
            f"{self._cfg.mqtt_discovery_prefix}/sensor/{self.node_id}/external_temp/config",
            f"{self.base}/external_temp",
        ):
            self._client.publish(topic, "", retain=True)
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
            elif component == "switch":
                payload["cmd_t"] = f"{self.base}/{key}/set"
                payload["pl_on"] = "on"
                payload["pl_off"] = "off"
                payload["stat_on"] = "on"
                payload["stat_off"] = "off"
            if key == BATTERY_ENTITY_KEY:
                payload["json_attr_t"] = f"{self.base}/{key}/attributes"
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
            "ent_cat": "config",
        }
        switch_topic = (
            f"{self._cfg.mqtt_discovery_prefix}/switch/{self.node_id}/pairing/config"
        )
        self._client.publish(switch_topic, json.dumps(switch), retain=True)
        log.info(
            "published HA discovery for %d entities under %s (incl. %d switches)",
            len(SENSORS),
            self._cfg.mqtt_discovery_prefix,
            sum(1 for k in SENSORS if SENSORS[k][0] == "switch"),
        )

    def set_availability(self, online: bool) -> None:
        self._client.publish(
            self.availability_topic, "online" if online else "offline", retain=True
        )

    def publish_pairing(self, paired: bool) -> None:
        self._client.publish(
            self.pairing_topic, "paired" if paired else "unpaired", retain=True
        )

    def publish_ble_state(self, state: str) -> None:
        self._client.publish(f"{self.base}/ble_pairing_state", state, retain=True)

    def publish_state(self, values: dict) -> None:
        # During boot / activation the controller reports garbage telemetry
        # (e.g. batt 0.27 V, SOC 572%, PV 82 V): report only the running state,
        # never the other entities, until it reaches a stable state.
        state = values.get("running_state")
        if state in ("power_on_delay", "battery_activated", "init"):
            self._client.publish(f"{self.base}/running_state", state, retain=True)
            return
        # The controller can also serve wrong-register frames while claiming a
        # healthy "running" state (ASCII version bytes decode as e.g.
        # batt 125.90 V / SOC 13102 %). Drop samples that are physically
        # impossible rather than letting them overwrite good retained values;
        # the next poll overwrites nothing and HA keeps the last sane data.
        bad = [
            f"{k}={v}"
            for k, v in (
                ("pv_voltage", values.get("pv_voltage")),
                ("battery_voltage", values.get("battery_voltage")),
                ("battery_soc", values.get("battery_soc")),
                ("pv_power", values.get("pv_power")),
                ("charge_power", values.get("charge_power")),
            )
            if v is not None and not PLAUSIBLE_RANGES[k][0] <= v <= PLAUSIBLE_RANGES[k][1]
        ]
        if bad:
            log.warning("dropping implausible sample (%s): %s", ", ".join(bad), state)
            return
        for key, value in values.items():
            if key not in SENSORS:
                continue
            self._client.publish(f"{self.base}/{key}", value, retain=True)
        attributes = {
            name: values[state_key]
            for state_key, name in BATTERY_ATTRIBUTES.items()
            if state_key in values
        }
        if attributes:
            self._client.publish(
                f"{self.base}/{BATTERY_ENTITY_KEY}/attributes",
                json.dumps(attributes),
                retain=True,
            )
