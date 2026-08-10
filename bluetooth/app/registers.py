"""Register map + decoders for the Limu/NOI LTM controller (see ../rs485/PROTOCOL.md).

Only *verified* contiguous public ranges are read as blocks: the firmware fails
an entire block read with exception 0x02 if ANY register in it is invalid.

Scaling (vendor manual): voltage x0.01 V, current x0.1 A, power x0.1 W,
temperature x0.1 C.
"""
from __future__ import annotations

# --- block definitions (start register, quantity) ---
BLOCK_SYSINFO = (0x000A, 0x1E)  # 0x000A-0x0027: product/model/versions/SN/vendor
BLOCK_RUNNING = (0x00A0, 0x10)  # 0x00A0-0x00AF: state, faults, PV, battery, charge
BLOCK_CONNECT = (0x0080, 0x0C)  # 0x0080-0x008B: GPS/BT/Wi-Fi/MQTT link status

# SOC raw unit is x0.1 % per the manual, but a raw value of 100 was read while
# the battery was effectively full (14.0 V on a 4S lithium pack), which makes
# x1 % (=> 100 %) far more plausible than x0.1 % (=> 10 %). Empirically chosen:
# flip this constant if the official app disagrees.
SOC_SCALE = 1.0

RUNNING_STATES = {
    0: "power_on_delay",
    1: "upgrading",
    2: "upgrade_failed",
    3: "init",
    4: "battery_activated",
    5: "running",
    6: "manual_shutdown",
}

CHARGE_PHASES = {
    0: "off",
    1: "fast",
    2: "equalize",
    3: "float",
    4: "balance",
    5: "mppt",
    6: "pause",
}

PRODUCT_IDS = {2: "LTB PWM", 3: "LTW WiFi PWM", 4: "LTM WiFi MPPT"}


def _ascii(regs: list[int]) -> str:
    """Decode 2-chars-per-register ASCII, stop at NUL."""
    raw = b"".join(r.to_bytes(2, "big") for r in regs)
    raw = raw.split(b"\x00")[0]
    return "".join(chr(b) if 32 <= b < 127 else "" for b in raw).strip()


def decode_sysinfo(regs: list[int]) -> dict:
    """Decode the 0x000A-0x0027 block (30 registers)."""
    pid = regs[0x000A - 0x000A]
    return {
        "product_id": pid,
        "product_type": PRODUCT_IDS.get(pid, f"unknown ({pid})"),
        "model": _ascii(regs[0x000B - 0x000A : 0x0011 - 0x000A]),
        "sw_version": _ascii(regs[0x0011 - 0x000A : 0x0014 - 0x000A]),
        "hw_version": _ascii(regs[0x0014 - 0x000A : 0x0017 - 0x000A]),
        "protocol_version": _ascii(regs[0x0017 - 0x000A : 0x001A - 0x000A]),
        "serial_number": _ascii(regs[0x001A - 0x000A : 0x0020 - 0x000A]),
        "manufacturer": _ascii(regs[0x0020 - 0x000A : 0x0028 - 0x000A]),
    }


def decode_running(regs: list[int]) -> dict:
    """Decode the 0x00A0-0x00AF block (16 registers) -> telemetry dict."""
    batt_type = regs[0x00A8 - 0x00A0]
    batt_kind = {(batt_type >> 14) & 0x3: None, 1: "lead_acid", 2: "lithium"}.get(
        (batt_type >> 14) & 0x3, "unknown"
    )
    phase_reg = regs[0x00AC - 0x00A0]
    return {
        "running_state": RUNNING_STATES.get(regs[0x00A0 - 0x00A0], "unknown"),
        "fault_code": regs[0x00A1 - 0x00A0],  # first of 4 fault bitmap words
        "pv_voltage": round(regs[0x00A5 - 0x00A0] * 0.01, 2),
        "pv_current": round(regs[0x00A6 - 0x00A0] * 0.1, 2),
        "pv_power": round(regs[0x00A7 - 0x00A0] * 0.1, 2),
        "battery_type": f"{batt_kind or 'unknown'} x{batt_type & 0xFF}",
        "battery_rated_voltage": regs[0x00A9 - 0x00A0],
        "battery_voltage": round(regs[0x00AA - 0x00A0] * 0.01, 2),
        "battery_soc": round(regs[0x00AB - 0x00A0] * SOC_SCALE, 1),
        "charge_phase": CHARGE_PHASES.get((phase_reg >> 8) & 0xFF, "unknown"),
        "charge_switch": "on" if (phase_reg & 0xFF) else "off",
        "charge_voltage": round(regs[0x00AD - 0x00A0] * 0.01, 2),
        "charge_current": round(regs[0x00AE - 0x00A0] * 0.1, 2),
        "charge_power": round(regs[0x00AF - 0x00A0] * 0.1, 2),
    }


def decode_connect(regs: list[int]) -> dict:
    """Decode the 0x0080-0x008B block (12 registers) -> link-status dict."""
    wifi = regs[0x0088 - 0x0080]
    rssi = (wifi >> 8) & 0xFF
    if rssi >= 128:
        rssi -= 256  # signed dBm
    return {
        "wifi_rssi": rssi,
        "wifi_connected": "on" if (wifi & 0xFF) else "off",
        "cloud_mqtt_connected": "on" if regs[0x008A - 0x0080] else "off",
        "cloud_mqtt_subscribed": "on" if regs[0x008B - 0x0080] else "off",
    }
