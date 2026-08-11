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
BLOCK_EXT = (0x00B0, 0x0C)  # 0x00B0-0x00BB: load/USB outlets, controller temp, fan
BLOCK_STATS = (0x0300, 0x14)  # 0x0300-0x0313: totals + today's stats

# writable output switches: state key -> register (on/off, single reg).
SWITCH_REGISTERS = {
    "load_switch": 0x00B0,
    "usb_switch": 0x00B4,
    "fan_switch": 0x00BA,
}

# SOC raw unit is x0.1 % per the manual, but a raw value of 100 was read while
# the battery was effectively full (14.0 V on a 4S lithium pack), which makes
# x1 % (=> 100 %) far more plausible than x0.1 % (=> 10 %). Empirically chosen:
# flip this constant if the official app disagrees.
SOC_SCALE = 1.0

# Controller temperature is x0.1 C per the manual (raw 304 => 30.4 C).
TEMP_SCALE = 0.1

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
    """Decode the 0x00A0-0x00AF block (16 registers) -> telemetry dict.

    Battery rated voltage is derived from the battery type register (0x00A8)
    rather than 0x00A9, which reads 1 with no app equivalent. For lithium
    (LiFePO4) cells at 3.2 V each and lead-acid cells at 2.0 V each, the
    rated voltage = cell_count * cell_voltage. Charge power/current are signed
    (a discharge shows as a negative raw word, e.g. 0xFFC8 = -56 => -5.6 W).
    """
    batt_type = regs[0x00A8 - 0x00A0]
    batt_kind = {(batt_type >> 14) & 0x3: None, 1: "lead_acid", 2: "lithium"}.get(
        (batt_type >> 14) & 0x3, "unknown"
    )
    batt_cells = batt_type & 0xFF
    if batt_kind == "lithium":
        rated_v = round(batt_cells * 3.2, 1)
    elif batt_kind == "lead_acid":
        rated_v = round(batt_cells * 2.0, 1)
    else:
        rated_v = None
    phase_reg = regs[0x00AC - 0x00A0]
    return {
        "running_state": RUNNING_STATES.get(regs[0x00A0 - 0x00A0], "unknown"),
        "fault_code": regs[0x00A1 - 0x00A0],  # first of 4 fault bitmap words
        "pv_voltage": round(regs[0x00A5 - 0x00A0] * 0.01, 2),
        "pv_current": round(regs[0x00A6 - 0x00A0] * 0.1, 2),
        "pv_power": round(regs[0x00A7 - 0x00A0] * 0.1, 2),
        "battery_type": f"{batt_kind or 'unknown'} x{batt_cells}",
        "battery_rated_voltage": rated_v,
        "battery_voltage": round(regs[0x00AA - 0x00A0] * 0.01, 2),
        "battery_soc": int(round(regs[0x00AB - 0x00A0] * SOC_SCALE)),
        "charge_phase": CHARGE_PHASES.get((phase_reg >> 8) & 0xFF, "unknown"),
        "charge_switch": "on" if (phase_reg & 0xFF) else "off",
        "charge_voltage": round(regs[0x00AD - 0x00A0] * 0.01, 2),
        "charge_current": round(_s16(regs[0x00AE - 0x00A0]) * 0.1, 2),
        "charge_power": round(_s16(regs[0x00AF - 0x00A0]) * 0.1, 2),
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


def decode_extension(regs: list[int]) -> dict:
    """Decode the 0x00B0-0x00BB block (12 registers) -> outlets/temp/fan.

    Live reads verified every register in this range is valid and readable.
    Block layout: load switch+VAW (0xB0-3), USB switch+VAW (0xB4-7),
    controller temp (0xB8, x0.1 C), fan switch+speed (0xBA-BB).
    The external sensor (0xB9) is OPTIONAL: it reads 0xFFFF when absent, so it
    is not published (no sensor fitted on this unit).
    """
    return {
        "load_switch": "on" if regs[0x00B0 - 0x00B0] else "off",
        "load_voltage": round(regs[0x00B1 - 0x00B0] * 0.01, 2),
        "load_current": round(regs[0x00B2 - 0x00B0] * 0.01, 2),
        "load_power": round(regs[0x00B3 - 0x00B0] * 0.1, 1),
        "usb_switch": "on" if regs[0x00B4 - 0x00B0] else "off",
        "usb_voltage": round(regs[0x00B5 - 0x00B0] * 0.01, 2),
        "usb_current": round(regs[0x00B6 - 0x00B0] * 0.01, 2),
        "usb_power": round(regs[0x00B7 - 0x00B0] * 0.1, 1),
        "controller_temp_c": round(regs[0x00B8 - 0x00B0] * TEMP_SCALE, 1),
        "fan_switch": "on" if regs[0x00BA - 0x00B0] else "off",
        "fan_speed": regs[0x00BB - 0x00B0],
    }


def _s16(v: int) -> int:
    """Interpret a 16-bit word as signed (two's complement)."""
    return v - 0x10000 if v > 0x7FFF else v


def _u16_to_u32(hi: int, lo: int) -> int:
    return (hi << 16) | lo


def decode_stats(regs: list[int]) -> dict:
    """Decode the 0x0300-0x0313 statistics block (20 registers).

    Totals are 32-bit and stored in Wh despite the manual's "0.1 kWh" label
    (a 32-bit word was measured at 1,835,148 = 1835.1 kWh), so kWh = raw/1000.
    Every today value is a SINGLE 16-bit register (manual page-8/9 table):
    energies are x0.1 Wh (=> kWh = raw/10000), voltages x0.01 V, currents and
    powers x0.1 A / x0.1 W.
    """
    def energy(hi_idx: int, lo_idx: int) -> float:
        return round(_u16_to_u32(regs[hi_idx], regs[lo_idx]) / 1000.0, 1)

    def energy_today(idx: int) -> float:
        return round(regs[idx] * 0.1 / 1000.0, 4)

    return {
        "total_runtime_s": int(_u16_to_u32(regs[0x0300 - 0x0300], regs[0x0301 - 0x0300])),
        "total_generation_kwh": energy(0x0302 - 0x0300, 0x0303 - 0x0300),
        "total_consumption_kwh": energy(0x0304 - 0x0300, 0x0305 - 0x0300),
        "full_charge_count": regs[0x0306 - 0x0300],
        "over_discharge_count": regs[0x0307 - 0x0300],
        "today_generation_kwh": energy_today(0x0308 - 0x0300),
        "today_max_pv_v": round(regs[0x0309 - 0x0300] * 0.01, 2),
        "today_max_pv_a": round(regs[0x030A - 0x0300] * 0.1, 2),
        "today_max_pv_w": round(regs[0x030B - 0x0300] * 0.1, 1),
        "today_max_batt_v": round(regs[0x030C - 0x0300] * 0.01, 2),
        "today_min_batt_v": round(regs[0x030D - 0x0300] * 0.01, 2),
        "today_consumption_kwh": energy_today(0x030E - 0x0300),
        "today_max_load_a": round(regs[0x030F - 0x0300] * 0.1, 2),
        "today_max_load_w": round(regs[0x0310 - 0x0300] * 0.1, 1),
        "today_usb_consumption_kwh": energy_today(0x0311 - 0x0300),
        "today_max_usb_a": round(regs[0x0312 - 0x0300] * 0.1, 2),
        "today_max_usb_w": round(regs[0x0313 - 0x0300] * 0.1, 1),
    }
