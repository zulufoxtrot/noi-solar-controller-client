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
    scan_timeout: float = 35.0,  # adverts arrive every ~25 s; 20 s can miss them
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


def _bluez_adapter(device) -> str:
    """Best-guess BlueZ adapter name (e.g. 'hci0') from a device's D-Bus path."""
    path = getattr(device, "details", {}) or {}
    parts = str(path.get("path", "")).split("/")
    return parts[3] if len(parts) > 3 else ""


async def release_stale_link(
    address: str, adapter: str = "", *, remove: bool = False
) -> bool:
    """Best-effort release of a controller link BlueZ may still hold.

    If a previous client died without calling Disconnect (e.g. a hard container
    restart), the host's BlueZ keeps the HCI connection up. This controller is
    single-link and stops advertising while connected, so a leftover link makes
    it invisible to scans forever. Disconnecting the device lets it resume
    advertising; with `remove=True` the device is also forgotten from BlueZ,
    clearing any stale GATT cache (which fixes "failed to discover services").

    Returns True if BlueZ reported the device connected before we acted.
    Never raises; a no-op without an address or when BlueZ is unavailable
    (e.g. non-Linux, where the lazy dbus_fast import fails).
    """
    if not address:
        return False
    try:
        from bleak.backends.bluezdbus.manager import get_global_bluez_manager
        from bleak.backends.bluezdbus import defs
        from dbus_fast import Message

        manager = await get_global_bluez_manager()
        adapter_path = (
            f"/org/bluez/{adapter}" if adapter else manager.get_default_adapter()
        )
        device_path = f"{adapter_path}/dev_{address.replace(':', '_').upper()}"
        connected = manager.is_connected(device_path)
        if remove:
            log.info("forgetting controller %s (%s) from BlueZ", address, device_path)
            await manager._bus.call(  # noqa: SLF001 - same pattern as bleak's own client
                Message(
                    destination=defs.BLUEZ_SERVICE,
                    interface=defs.ADAPTER_INTERFACE,
                    path=adapter_path,
                    member="RemoveDevice",
                    signature="o",
                    body=[device_path],
                )
            )
        elif connected:
            log.info("releasing stale BlueZ link to %s (%s)", address, device_path)
            await manager._bus.call(  # noqa: SLF001 - same pattern as bleak's own client
                Message(
                    destination=defs.BLUEZ_SERVICE,
                    interface=defs.DEVICE_INTERFACE,
                    path=device_path,
                    member="Disconnect",
                )
            )
        return connected
    except Exception as exc:  # noqa: BLE001 - best-effort, diagnostics only
        log.debug("stale-link cleanup skipped: %s", exc)
        return False


class BleModbusClient:
    """Async Modbus-RTU-over-BLE client (one outstanding request at a time).

    Telemetry registers are ungated when the controller is healthy, so no PIN
    is ever written proactively. If a read comes back EXC 0x04 (auth-gated,
    seen after too-rapid reconnects on fw 2.0.4), `pin` decides the strategy:
    with a PIN configured the client presents it once via FC10 @ 0x0400 and
    retries patiently; with no PIN it fails fast so the caller backs off.
    On fw 2.0.4 the PIN write itself makes the controller drop the BLE link
    within ~2 s, so leave `BLE_PIN` empty unless older firmware needs it.
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
        self._expecting: bool | None = False  # True while a response is awaited

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
        if self._expecting is not True:
            return  # unsolicited/stale response, drop it
        self._buf.extend(data)
        self._data.set()

    async def connect(self) -> None:
        await release_stale_link(self.device.address, _bluez_adapter(self.device))
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
        log.info("connected to %r (%s)", self.device.name, self.device.address)
        # No eager PIN write: the vendor app never writes a PIN in its polling path
        # (verified on live captures, Aug-10) — it just subscribes and reads.
        # Writing the PIN eagerly makes this controller drop the BLE link right
        # after connect (observed ~2 s later, every connect), so skip it and
        # match the app exactly. A lazy unlock remains as a backstop if a read
        # does come back auth-gated.

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
        self._expecting = True
        self._buf.clear()
        self._data.clear()
        try:
            await self._client.write_gatt_char(self._write_char, frame, response=False)
        except Exception:
            self._expecting = False
            raise

    async def _wait_frame(self, timeout: float | None = None) -> int:
        wait = self.read_timeout if timeout is None else timeout
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait
        try:
            while True:
                exp = modbus.expected_response_length(self._buf)
                if exp is not None and len(self._buf) >= exp:
                    return exp
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError(
                        f"request timed out after {wait}s"
                    )
                try:
                    await asyncio.wait_for(self._data.wait(), remaining)
                    self._data.clear()
                except asyncio.TimeoutError:
                    pass  # re-check buffer/deadline
        finally:
            self._expecting = False

    async def _unlock(self, timeout: float | None = None) -> None:
        """Present the PIN as ASCII regs @ 0x0400 (FC10). The device errors 0x02
        (sometimes 0x04) on the write yet still honours it, so any response is
        treated as success. Called as the first Modbus frame after connect; also
        re-called lazily when a read is still auth-gated."""
        log.info("presenting BLE PIN %r to %#06x", self.pin, self.PIN_REG)
        await self._send(modbus.build_write_multi(self.PIN_REG, self._ascii_regs(self.pin)))
        try:
            await self._wait_frame(timeout)
        except (modbus.ModbusError, TimeoutError):
            pass  # 0x02/0x04 rejection / no ack is expected
        # The controller answers the PIN write late (~4 s); its 0x02/0x04
        # rejection must not be left in the buffer, or it is mistaken for the
        # next read's response (a "fc=0x10 code=0x04" session failure).
        self._buf.clear()
        # The gate opens late: reads right after the write still come back
        # EXC 0x04. Stay quiet briefly so the acceptance lands (and any stray
        # rejection drains), then clear again before the caller retries.
        await asyncio.sleep(4.0)
        self._buf.clear()
        self._unlocked = True

    async def read_holding(self, start: int, qty: int) -> list[int]:
        """FC03 read; unlocks first if the register is auth-gated."""
        async with self._lock:
            for attempt in (0, 1, 2):
                await self._send(modbus.build_read(start, qty))
                try:
                    exp = await self._wait_frame()
                    return modbus.parse_read_response(bytes(self._buf[:exp]))
                except modbus.ModbusError as exc:
                    if exc.code != self.AUTH_EXC or attempt == 2 or not self.pin:
                        # No PIN configured: never write it — on fw 2.0.4 the
                        # PIN write makes the controller drop the BLE link
                        # within ~2 s. Back off instead and let the gate lift.
                        raise
                    if attempt == 0 and not self._unlocked:
                        await self._unlock()
                    else:
                        # Already unlocked once: the gate just opens slowly.
                        self._buf.clear()
                        await asyncio.sleep(4.0)
                    continue
            raise RuntimeError("unreachable")

    async def write_holding(self, start: int, value: int) -> None:
        """FC06/FC10 write a single register (e.g. a load/USB/fan switch).

        Uses the multi-register frame for one value; unlocks first if the
        register is auth-gated, same retry pattern as reads.
        """
        async with self._lock:
            for attempt in (0, 1, 2):
                await self._send(modbus.build_write_multi(start, [value]))
                try:
                    exp = await self._wait_frame()
                    modbus.parse_ack(bytes(self._buf[:exp]))
                    return
                except modbus.ModbusError as exc:
                    if exc.code != self.AUTH_EXC or attempt == 2 or not self.pin:
                        raise
                    if attempt == 0 and not self._unlocked:
                        await self._unlock()
                    else:
                        # Already unlocked once: the gate just opens slowly.
                        self._buf.clear()
                        await asyncio.sleep(4.0)
                    continue
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
                5, 0, 0, 0, 0, 2109, 3, 76, 0x8004, 1, 1400, 100, 0x0105, 2092, 1, 0xFFC8
            ],
            0x00B0: [1, 1200, 25, 30, 0, 500, 10, 5, 304, 0xFFFF, 0, 0],
            0x0300: self._stats_block(),
            0x0504: [0, 0, 0, 0],  # command/session area (vendor polls it)
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

    @staticmethod
    def _stats_block() -> list[int]:
        regs = [0] * 0x14  # 0x0300-0x0313
        regs[0x0300 - 0x0300] = 0            # runtime u32 hi
        regs[0x0301 - 0x0300] = 8            # runtime u32 lo (= 8 s)
        regs[0x0302 - 0x0300] = 0x001C       # total gen u32 hi
        regs[0x0303 - 0x0300] = 0x008C       # total gen u32 lo (= 1835.1 kWh)
        regs[0x0306 - 0x0300] = 128          # full-charge count
        regs[0x0307 - 0x0300] = 36           # over-discharge count
        regs[0x0308 - 0x0300] = 1669         # today gen (166.9 Wh, x0.1 Wh)
        regs[0x0309 - 0x0300] = 2397         # today max PV V (23.97)
        regs[0x030A - 0x0300] = 25           # today max PV A (2.5)
        regs[0x030B - 0x0300] = 530          # today max PV W (53.0)
        regs[0x030C - 0x0300] = 1429         # today max batt V (14.29)
        regs[0x030D - 0x0300] = 566          # today min batt V (5.66 — suspect register)
        regs[0x030E - 0x0300] = 36           # today max load A (0.36, x0.01 A)
        regs[0x0310 - 0x0300] = 191          # today battery discharge (19.1 Wh)
        regs[0x0312 - 0x0300] = 0            # today max USB A (0.0)
        regs[0x0313 - 0x0300] = 490          # today consumption (49.0 Wh, x0.1 Wh)
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
        for base, block in self._blocks.items():
            if base <= start and start + qty <= base + len(block):
                off = start - base
                await asyncio.sleep(0.05)
                return list(block[off : off + qty])
        raise modbus.ModbusError(f"SIMULATE: no canned block at {start:#06x}+{qty}")

    async def write_holding(self, start: int, value: int) -> None:
        """Simulated switch write: mutates the canned block so a subsequent
        read reflects the new value (exercises the full MQTT toggle path)."""
        if not self._connected:
            raise ConnectionError("not connected")
        for base, block in self._blocks.items():
            if base <= start < base + len(block):
                reg = list(block)
                reg[start - base] = value & 0xFFFF
                self._blocks[base] = reg
                return
        raise modbus.ModbusError(f"SIMULATE: no writable register at {start:#06x}")
