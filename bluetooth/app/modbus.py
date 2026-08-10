"""Modbus RTU framing for the Limu/NOI LTM controller.

Verified on-device (see ../rs485/PROTOCOL.md):
  * frame = [addr][FC][data][CRC16]
  * slave address 0xFF = all-purpose single slave
  * CRC16-Modbus (poly 0xA001, init 0xFFFF) stored **big-endian**
    (firmware contradicts the vendor manual)
"""
from __future__ import annotations

SLAVE_ADDR = 0xFF

# Modbus exception codes (subset documented by the vendor manual)
EXCEPTIONS = {
    0x01: "unsupported function",
    0x02: "illegal register address",
    0x03: "illegal value",
    0x04: "operation failed (auth-gated?)",
    0x05: "length error",
    0x06: "CRC failed",
    0x07: "read-only parameter",
    0x08: "wrong password",
}


class ModbusError(Exception):
    """Raised on exception frames and malformed responses."""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code  # exception code for exception frames, else None


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def build_read(start: int, qty: int, addr: int = SLAVE_ADDR, fc: int = 0x03) -> bytes:
    """Build an FC03/FC04 read request frame."""
    body = bytes(
        [addr, fc, (start >> 8) & 0xFF, start & 0xFF, (qty >> 8) & 0xFF, qty & 0xFF]
    )
    c = crc16(body)
    return body + bytes([(c >> 8) & 0xFF, c & 0xFF])  # big-endian CRC


def build_write_multi(start: int, values: list[int], addr: int = SLAVE_ADDR) -> bytes:
    """FC10 write-multiple-registers frame. values = 16-bit register words."""
    qty = len(values)
    data = b"".join(v.to_bytes(2, "big") for v in values)
    body = bytes(
        [addr, 0x10, (start >> 8) & 0xFF, start & 0xFF,
         (qty >> 8) & 0xFF, qty & 0xFF, len(data)]
    ) + data
    c = crc16(body)
    return body + bytes([(c >> 8) & 0xFF, c & 0xFF])


def build_write_single(start: int, value: int, addr: int = SLAVE_ADDR) -> bytes:
    """FC06 write-single-register frame."""
    body = bytes(
        [addr, 0x06, (start >> 8) & 0xFF, start & 0xFF,
         (value >> 8) & 0xFF, value & 0xFF]
    )
    c = crc16(body)
    return body + bytes([(c >> 8) & 0xFF, c & 0xFF])


def expected_response_length(buf: bytes | bytearray) -> int | None:
    """Total length of the response frame being accumulated, None if unknown yet.

    BLE notifications may arrive fragmented; accumulate until this length.
    """
    if len(buf) < 3:
        return None
    fc = buf[1]
    if fc & 0x80:  # exception: addr fc code crc(2)
        return 5
    if fc == 0x03 or fc == 0x04:  # addr fc bytecount data crc(2)
        return 3 + buf[2] + 2
    if fc in (0x06, 0x10):  # write ack: addr fc reg(2) qty/val(2) crc(2)
        return 8
    return None


def parse_read_response(raw: bytes) -> list[int]:
    """Validate an FC03 response frame and return the register values."""
    if len(raw) < 5:
        raise ModbusError(f"short frame: {raw.hex()}")
    if raw[1] & 0x80:
        code = raw[2]
        raise ModbusError(
            f"modbus exception fc={raw[1] & 0x7F:#04x} code={code:#04x} "
            f"({EXCEPTIONS.get(code, 'unknown')})",
            code=code,
        )
    if raw[1] != 0x03:
        raise ModbusError(f"unexpected function code {raw[1]:#04x}")
    (crc_hi, crc_lo), body = raw[-2:], raw[:-2]
    if crc16(body) != (crc_hi << 8 | crc_lo):
        raise ModbusError(f"CRC mismatch in {raw.hex()}")
    n = raw[2]
    data = raw[3 : 3 + n]
    if len(data) != n or n % 2:
        raise ModbusError(f"bad byte count in {raw.hex()}")
    return [int.from_bytes(data[i : i + 2], "big") for i in range(0, n, 2)]


def parse_ack(raw: bytes) -> None:
    """Validate an FC06/FC10 write acknowledgement (8-byte echo)."""
    if len(raw) < 5:
        raise ModbusError(f"short write ack: {raw.hex()}")
    if raw[1] & 0x80:
        raise ModbusError(
            f"write rejected fc={raw[1] & 0x7F:#04x} code={raw[2]:#04x} "
            f"({EXCEPTIONS.get(raw[2], 'unknown')})"
        )
    if raw[1] not in (0x06, 0x10) or len(raw) != 8:
        raise ModbusError(f"unexpected write ack: {raw.hex()}")
    (crc_hi, crc_lo), body = raw[-2:], raw[:-2]
    if crc16(body) != (crc_hi << 8 | crc_lo):
        raise ModbusError(f"CRC mismatch in write ack {raw.hex()}")
