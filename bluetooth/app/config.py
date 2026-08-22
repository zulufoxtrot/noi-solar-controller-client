"""Configuration from environment variables (12-factor / docker-friendly)."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _bool(key: str, default: bool = False) -> bool:
    return os.environ.get(key, str(default)).lower() in ("1", "true", "yes", "on")


def _env(key: str, default: str) -> str:
    value = os.environ.get(key)
    return value if value and value.strip() else default


@dataclass
class Config:
    # --- MQTT broker ---
    mqtt_host: str = "127.0.0.1"
    mqtt_port: int = 1883
    mqtt_username: str | None = None
    mqtt_password: str | None = None
    mqtt_topic_prefix: str = "limu_solar"
    mqtt_discovery_prefix: str = "homeassistant"
    mqtt_client_id: str = "limu-solar-poc"

    # --- controller discovery / BLE ---
    controller_name_prefix: str = "LTM-"  # adverts: LTM-<last 6 of SN>
    controller_address: str = ""  # optional MAC/UUID to skip scanning
    ble_adapter: str = ""  # e.g. "hci0" (Linux/BlueZ only)
    ble_pin: str = "000000"  # unlock PIN (FC10 ASCII write @ 0x0400)
    scan_timeout: float = 20.0
    ble_timeout: float = 10.0
    read_timeout: float = 5.0

    # --- behaviour ---
    poll_interval: float = 30.0
    retry_interval: float = 15.0
    retry_backoff_max: float = 300.0  # cap for exponential backoff after repeated failures
    # The controller's BLE tunnel wedges ~90 s into every connection (sw 2.0.4:
    # reads stop being answered and the radio then hides for minutes). Rotate
    # the link proactively before that point; 0 disables rotation.
    max_session_seconds: float = 75.0
    # Quiet gap between a planned rotation and the next connect; reconnecting
    # within seconds of a clean disconnect trips its auth gate.
    rotate_gap_seconds: float = 20.0
    # Silence window after connect before the first Modbus request.
    post_connect_seconds: float = 8.0
    device_name: str = "Limu Solar Controller"  # display name in HA
    simulate: bool = False  # fake telemetry, no BLE (test MQTT path)
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            mqtt_host=_env("MQTT_HOST", cls.mqtt_host),
            mqtt_port=int(_env("MQTT_PORT", str(cls.mqtt_port))),
            mqtt_username=os.environ.get("MQTT_USERNAME") or None,
            mqtt_password=os.environ.get("MQTT_PASSWORD") or None,
            mqtt_topic_prefix=_env(
                "MQTT_TOPIC_PREFIX", cls.mqtt_topic_prefix
            ).strip("/"),
            mqtt_discovery_prefix=_env(
                "MQTT_DISCOVERY_PREFIX", cls.mqtt_discovery_prefix
            ).strip("/"),
            mqtt_client_id=_env("MQTT_CLIENT_ID", cls.mqtt_client_id),
            controller_name_prefix=_env(
                "CONTROLLER_NAME_PREFIX", cls.controller_name_prefix
            ),
            controller_address=os.environ.get("CONTROLLER_ADDRESS", ""),
            ble_adapter=os.environ.get("BLE_ADAPTER", ""),
            ble_pin=_env("BLE_PIN", cls.ble_pin),
            scan_timeout=float(_env("SCAN_TIMEOUT", str(cls.scan_timeout))),
            ble_timeout=float(_env("BLE_TIMEOUT", str(cls.ble_timeout))),
            read_timeout=float(_env("READ_TIMEOUT", str(cls.read_timeout))),
            poll_interval=float(_env("POLL_INTERVAL", str(cls.poll_interval))),
            retry_interval=float(_env("RETRY_INTERVAL", str(cls.retry_interval))),
            retry_backoff_max=float(
                _env("RETRY_BACKOFF_MAX", str(cls.retry_backoff_max))
            ),
            max_session_seconds=float(
                _env("MAX_SESSION_SECONDS", str(cls.max_session_seconds))
            ),
            rotate_gap_seconds=float(
                _env("ROTATE_GAP_SECONDS", str(cls.rotate_gap_seconds))
            ),
            post_connect_seconds=float(
                _env("POST_CONNECT_SECONDS", str(cls.post_connect_seconds))
            ),
            device_name=_env("DEVICE_NAME", cls.device_name),
            simulate=_bool("SIMULATE", False),
            log_level=os.environ.get("LOG_LEVEL", cls.log_level).upper(),
        )
