"""BLE transport: Modbus RTU over GATT (service FFF1), via bleak.

GATT layout (verified, see BLE.md — roles are opposite of the names):
  * characteristic AAA1 = NOTIFY    (device -> client responses)
  * characteristic BBB1 = WriteWithoutResponse (client -> device requests)

bleak uses CoreBluetooth on macOS and BlueZ/D-Bus on Linux, so the same code
runs natively on a Mac and inside a Linux container with the host's BlueZ.
"""
from __future__ import annotations

import asyncio
import logging

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice

from . import modbus

log = logging.getLogger(__name__)


def _short16(uuid: str) -> str:
    """'0000AAA1-...' / 'AAA1' -> 'AAA1' (tolerates any 128-bit base)."""
    u = uuid.upper().replace("-", "")
    return u if len(u) == 4 else u[4:8]


async def discover_controller(
    name_prefix: str,
    address: str = "",
    adapter: str = "",
    scan_timeout: float = 20.0,
) -> BLEDevice | None:
    """Auto-discover the controller by advertised name prefix (or address).

    Returns the strongest-RSSI match; None if nothing was found.
    """
    found: dict[str, tuple[BLEDevice, int, str]] = {}

    def on_detect(device: BLEDevice, adv) -> None:
        name = (device.name or getattr(adv, "local_name", None) or "").strip()
        if address and device.address.upper() == address.upper():
            found[device.address] = (device, adv.rssi or -999, name)
        elif not address and name.startswith(name_prefix):
            if device.address not in found:
                log.info("discovered %r at %s (RSSI %s dBm)", name, device.address, adv.rssi)
            found[device.address] = (device, adv.rssi or -999, name)

    kwargs: dict = {"detection_callback": on_detect}
    if adapter:
        kwargs["adapter"] = adapter  # Linux/BlueZ only, e.g. "hci0"
    scanner = BleakScanner(**kwargs)
    log.info(
        "scanning %.0fs for controller (%s) ...",
        scan_timeout,
        f"address {address}" if address else f"name prefix {name_prefix!r}",
    )
    await scanner.start()
    await asyncio.sleep(scan_timeout)
    await scanner.stop()

    if not found:
        return None
    device, rssi, name = max(found.values(), key=lambda t: t[1])
    log.info("selected %r at %s (RSSI %s dBm)", name, device.address, rssi)
    return device


class BleModbusClient:
    """Async Modbus-RTU-over-BLE client (one outstanding request at a time).

    The controller gates BLE access (reads return EXC 0x04) — increasingly so
    on reconnects — behind a PIN lock (default "000000"). This client presents
    the PIN as ASCII registers via FC10 @ 0x0400 immediately after connect,
    so reads are un-gated from the first one. If a read still comes back EXC
    0x04 (e.g. wrong PIN), it re-presents the PIN once and retries while the
    gate is not yet open. Note the controller acknowledges the PIN write with
    an illegal-register exception (0x02) or auth exception (0x04) while still
    acting on it, so the unlock write is fire-and-forget.
    """

    PIN_REG = 0x0400  # register receiving the ASCII PIN
    AUTH_EXC = 0x04   # exception meaning "auth-gated"

    def __init__(
        self,
        device: BLEDevice,
        ble_timeout: float = 10.0,
        read_timeout: float = 5.0,
        pin: str = "000000",
    ):
        self.device = device
        self.pin = pin
        self.read_timeout = read_timeout
        self._ble_timeout = ble_timeout
        self._client: BleakClient | None = None
        self._write_char = None
        self._buf = bytearray()
        self._data = asyncio.Event()
        self._lock = asyncio.Lock()
        self._connected = False
        self._unlocked = False

    @property
    def name(self) -> str:
        return self.device.name or self.device.address

    @property
    def is_connected(self) -> bool:
        return self._connected and self._client is not None and self._client.is_connected

    def _on_disconnect(self, _client) -> None:
        log.warning("BLE link lost")
        self._connected = False

    def _on_notify(self, _char, data: bytearray) -> None:
        self._buf.extend(data)
        self._data.set()

    async def connect(self) -> None:
        self._client = BleakClient(
            self.device,
            disconnected_callback=self._on_disconnect,
            timeout=self._ble_timeout,
        )
        await self._client.connect()
        notify_char = write_char = None
        for service in self._client.services:
            if _short16(str(service.uuid)) != "FFF1":
                continue
            for char in service.characteristics:
                short = _short16(str(char.uuid))
                if short == "AAA1":
                    notify_char = char
                elif short == "BBB1":
                    write_char = char
        if notify_char is None or write_char is None:
            await self._client.disconnect()
            raise RuntimeError("Modbus GATT characteristics (AAA1/BBB1) not found")
        self._write_char = write_char
        await self._client.start_notify(notify_char, self._on_notify)
        self._connected = True
        log.info("connected to %r (%s)", self.name, self.device.address)
        # The controller re-gates the link right after connect (0x04), especially
        # on a reconnect/re-pair, so present the PIN as soon as the link is up.
        # Unlock persists across reconnects, so re-sending is harmless/idempotent.
        await self._unlock()

    async def disconnect(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:  # noqa: BLE001 - already gone is fine
                pass
        self._connected = False

    def _ascii_regs(self, s: str) -> list[int]:
        raw = s.encode("ascii")[:6].ljust(6, b"\x00")
        return [int.from_bytes(raw[i : i + 2], "big") for i in range(0, 6, 2)]

    async def _send(self, frame: bytes) -> None:
        if not self.is_connected:
            raise ConnectionError("not connected")
        self._buf.clear()
        self._data.clear()
        await self._client.write_gatt_char(self._write_char, frame, response=False)

    async def _wait_frame(self) -> int:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.read_timeout
        while True:
            exp = modbus.expected_response_length(self._buf)
            if exp is not None and len(self._buf) >= exp:
                return exp
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(
                    f"request timed out after {self.read_timeout}s"
                )
            try:
                await asyncio.wait_for(self._data.wait(), remaining)
                self._data.clear()
            except asyncio.TimeoutError:
                pass  # re-check buffer/deadline

    async def _unlock(self) -> None:
        """Present the PIN as ASCII regs @ 0x0400 (FC10). The device errors 0x02
        (sometimes 0x04) on the write yet still honours it, so any response is
        treated as success. Called ASAP after every connect."""
        log.info("presenting BLE PIN %r to %#06x", self.pin, self.PIN_REG)
        await self._send(modbus.build_write_multi(self.PIN_REG, self._ascii_regs(self.pin)))
        try:
            await self._wait_frame()
        except (modbus.ModbusError, TimeoutError):
            pass  # 0x02/0x04 rejection / no ack is expected
        self._unlocked = True

    async def read_holding(self, start: int, qty: int) -> list[int]:
        """FC03 read; unlocks first if the register is auth-gated."""
        async with self._lock:
            for attempt in (0, 1):
                await self._send(modbus.build_read(start, qty))
                try:
                    exp = await self._wait_frame()
                    return modbus.parse_read_response(bytes(self._buf[:exp]))
                except modbus.ModbusError as exc:
                    if (
                        attempt == 0
                        and not self._unlocked
                        and exc.code == self.AUTH_EXC
                    ):
                        await self._unlock()
                        continue
                    raise
            raise RuntimeError("unreachable")


class SimulatedModbusClient:
    """Fake controller for testing the MQTT/HA path without BLE hardware.

    Canned register values mirror the verified live capture in ../rs485/PROTOCOL.md.
    """

    def __init__(self):
        self.name = "LTM-252245 (simulated)"
        self._connected = False
        self._blocks = {
            0x000A: self._sysinfo_block(),
            0x0080: [0, 0, 0, 0, 0, 0, 0, 0, 0xCA01, 0, 1, 1],
            0x00A0: [
                5, 0, 0, 0, 0, 2109, 3, 76, 0x8004, 1, 1400, 100, 0x0105, 2092, 1, 23
            ],
        }

    @staticmethod
    def _ascii_regs(s: str, nregs: int) -> list[int]:
        raw = s.encode()[: nregs * 2].ljust(nregs * 2, b"\x00")
        return [int.from_bytes(raw[i : i + 2], "big") for i in range(0, len(raw), 2)]

    @classmethod
    def _sysinfo_block(cls) -> list[int]:
        regs = [0] * 0x1E
        regs[0x000A - 0x000A] = 4
        for base, s, n in (
            (0x000B, "LTM2430", 6),
            (0x0011, "1.0.8", 3),
            (0x0014, "1.3.2", 3),
            (0x0017, "1.0.2", 3),
            (0x001A, "000629252245", 6),
            (0x0020, "LimuTech", 8),
        ):
            regs[base - 0x000A : base - 0x000A + n] = cls._ascii_regs(s, n)
        return regs

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        log.info("SIMULATE: connected to fake controller")
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def read_holding(self, start: int, qty: int) -> list[int]:
        if not self._connected:
            raise ConnectionError("not connected")
        block = self._blocks.get(start)
        if block is None or len(block) != qty:
            raise modbus.ModbusError(f"SIMULATE: no canned block at {start:#06x}+{qty}")
        await asyncio.sleep(0.05)
        return list(block)
