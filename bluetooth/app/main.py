"""Main loop: discover controller -> poll telemetry over BLE -> publish to MQTT.

    LTM-252245 (BLE GATT FFF1)  --Modbus RTU-->  this bridge  --MQTT-->  Home Assistant
"""
from __future__ import annotations

import asyncio
import logging
import signal

from . import modbus
from . import registers
from .ble_client import (
    BleModbusClient,
    SimulatedModbusClient,
    discover_controller,
    release_stale_link,
)
from .config import Config
from .mqtt_client import MqttBridge

log = logging.getLogger(__name__)


async def _disconnect_soft(client, timeout: float) -> None:
    """Best-effort BLE disconnect that can never hang the caller.

    BlueZ/D-Bus disconnects can stall (especially in containers sharing the
    host's socket); a hung disconnect must not wedge the pairing state machine
    or shutdown, so it is always bounded and swallowed.
    """
    if client is None:
        return
    try:
        await asyncio.wait_for(client.disconnect(), timeout)
    except asyncio.TimeoutError:
        log.warning("BLE disconnect timed out after %.0fs", timeout)
    except Exception as exc:  # noqa: BLE001 - link may already be gone
        log.warning("BLE disconnect failed: %s", exc)


def _retry_backoff(retry_interval: float, streak: int, cap: float) -> float:
    """Retry wait with exponential backoff, capped at `cap`.

    A controller stuck in its error-rate kick state needs minutes of quiet to
    recover; hammering it every `retry_interval` keeps it kicked. Grow the wait
    with each consecutive failure (1x, 2x, 4x, ...) up to `cap` seconds.
    """
    if streak <= 1:
        return retry_interval
    return min(retry_interval * (2 ** min(streak - 1, 8)), cap)


# The vendor app polls with small targeted reads (max 4 registers each:
# 0x00A0:1, 0x00A5:3, 0x0088:1, 0x0504:4). Large block reads (e.g. the
# 16-register 0x00A0 block) fragment into multiple BLE notifications and
# progressively degrade the controller's ATT stack, ending in a hard reset.
# Poll the same registers in the same order first — on fw 2.0.4 the tunnel
# frequently ignores reads that don't follow the vendor cadence — then append
# our extra battery/charge chunks once the session is warm.
RUNNING_SMALL_READS = [
    (0x00A0, 0x01),  # state (vendor-exact quantity)
    (0x00A5, 0x03),  # PV V / A / W
    (0x0088, 0x01),  # Wi-Fi / cloud linkage (vendor polls this)
    (0x0504, 0x04),  # command/session area (vendor polls this every cycle)
    (0x00A8, 0x04),  # battery type + rated + voltage + SOC
    (0x00AC, 0x04),  # charge phase/switch + charge V / A / W
]

# Extended telemetry groups, appended after the mandatory vendor prefix.
# NEVER reorder the prefix - the controller gates sessions whose first frames
# don't follow the vendor cadence. Each group below is all-or-nothing: if any
# of its chunks fails, the group's keys are simply skipped for this burst and
# HA keeps its previous retained values (never zeros). Group failures never
# fail the core sample.
OPTIONAL_GROUPS = [
    ("ext", registers.BLOCK_EXT[0], registers.BLOCK_EXT[1], registers.decode_extension,
     [(0x00B0, 0x04), (0x00B4, 0x04), (0x00B8, 0x04)]),
    ("stats", registers.BLOCK_STATS[0], registers.BLOCK_STATS[1], registers.decode_stats,
     [(0x0300, 0x04), (0x0304, 0x04), (0x0308, 0x04),
      (0x030C, 0x04), (0x0310, 0x04)]),
    ("connect", registers.BLOCK_CONNECT[0], registers.BLOCK_CONNECT[1], registers.decode_connect,
     [(0x0080, 0x04), (0x0084, 0x04), (0x0088, 0x04)]),
]


async def poll_once(client) -> dict:
    """Read the running-data block as small vendor-style reads."""
    regs = [0] * 0x10
    for start, qty in RUNNING_SMALL_READS:
        chunk = await client.read_holding(start, qty)
        await asyncio.sleep(0.5)
        for i, value in enumerate(chunk):
            idx = start - 0x00A0 + i
            if 0 <= idx < 0x10:
                regs[idx] = value
    state = {**registers.decode_running(regs)}

    # Optional second fault-bitmap word (vendor reads only 0x00A0 itself).
    try:
        state["fault_code"] = (await client.read_holding(0x00A1, 0x01))[0]
        await asyncio.sleep(0.5)
    except Exception as exc:  # noqa: BLE001 - optional telemetry
        log.debug("optional read 0x00A1 failed: %s", exc)

    for name, base, length, decoder, chunks in OPTIONAL_GROUPS:
        try:
            block = [0] * length
            covered = [False] * length
            for start, qty in chunks:
                chunk = await client.read_holding(start, qty)
                await asyncio.sleep(0.5)
                for i, value in enumerate(chunk):
                    block[start - base + i] = value
                    covered[start - base + i] = True
            if not all(covered):
                raise RuntimeError("incomplete block coverage")
            state.update(decoder(block))
        except Exception as exc:  # noqa: BLE001 - optional telemetry
            log.warning("optional %s block skipped this burst: %s", name, exc)
    return state


async def session(
    client, cfg: Config, mqtt: MqttBridge | None, box: dict, stop: asyncio.Event
) -> MqttBridge:
    """One paired session: sysinfo + discovery, then poll until an unpair
    command arrives, the BLE link drops, or shutdown is requested. Returns the
    (possibly new) bridge. `stop` is observed so SIGTERM unwinds to a clean
    `client.disconnect()` instead of leaving the BLE link held."""
    created = mqtt is None
    loop = asyncio.get_running_loop()
    try:
        if mqtt is None:
            # Identity is cached in `box` across sessions: a session that dies
            # before returning must not re-read the big sysinfo block and
            # rebuild discovery on every retry — repeated 30-register block
            # reads stress the controller's fragile ATT tunnel and keep it in
            # its error-rate kick state.
            sysinfo = box.get("sysinfo")
            if sysinfo is None:
                # Retry every session until it lands: without the serial the
                # bridge publishes under a fallback node id, forking the HA
                # device away from its whole history. Small chunks only - the
                # full 30-register block fragments into many notifications and
                # regularly overruns READ_TIMEOUT. This read is sysinfo-area,
                # so it doubles as the mandatory session opener.
                try:
                    regs = [0] * registers.BLOCK_SYSINFO[1]
                    for start, qty in ((0x000A, 0x06), (0x001A, 0x06)):
                        chunk = await client.read_holding(start, qty)
                        for i, value in enumerate(chunk):
                            regs[start - registers.BLOCK_SYSINFO[0] + i] = value
                    sysinfo = registers.decode_sysinfo(regs)
                    box["sysinfo"] = sysinfo
                except Exception as exc:  # noqa: BLE001 - identity is optional
                    log.warning(
                        "sysinfo read failed (%s); publishing under fallback id",
                        exc,
                    )
                    sysinfo = {}
            elif sysinfo is None:
                sysinfo = {}
            node_id = (
                box.get("node_id")
                or sysinfo.get("serial_number")
                or client.name.replace(" ", "_")
            )
            box["node_id"] = node_id
            if sysinfo:
                log.info(
                    "controller: %s %s (SN %s, sw %s)",
                    sysinfo.get("manufacturer"), sysinfo.get("model"),
                    node_id, sysinfo.get("sw_version"),
                )
            else:
                log.info("controller identity unknown; using %r", node_id)
            mqtt = MqttBridge(
                cfg, node_id, sysinfo,
                on_pair=box["on_pair"], on_command=box["on_command"],
            )
            mqtt.connect()
            mqtt.publish_discovery()
        mqtt.set_availability(True)
        mqtt.publish_pairing(True)
        mqtt.publish_ble_state("connected")

        # Session opener: the controller expects the FIRST Modbus frame of
        # every connection to be a sysinfo-area read (the vendor app does this
        # too). Skipping it - e.g. because identity is cached - makes fw 2.0.4
        # gate all further traffic with EXC 0x04 until some future session
        # opens correctly. This is a login, not identity retrieval.
        await client.read_holding(registers.BLOCK_SYSINFO[0], 0x04)

        # fw 2.0.4 stays silent for ~10 s after connect; reads fired earlier
        # simply time out. Let the tunnel wake up before the first request.
        if cfg.post_connect_seconds and not cfg.simulate:
            await asyncio.sleep(cfg.post_connect_seconds)

        # The controller's tunnel wedges ~90 s into a connection (sw 2.0.4);
        # rotating before that keeps every session healthy end-to-end.
        started = loop.time()
        while box["paired"] and client.is_connected and not stop.is_set():
            state = await poll_once(client)
            log.info(
                "PV %.2f V / %.1f W | batt %.2f V / %.1f%% | charge %s %.1f W | %s",
                state["pv_voltage"], state["pv_power"],
                state["battery_voltage"], state["battery_soc"],
                state["charge_phase"], state["charge_power"], state["running_state"],
            )
            mqtt.publish_state(state)
            box["polled"] = True
            if cfg.burst_mode:
                # One sample is all we want: leave before the controller's
                # link layer kicks us anyway.
                log.info("burst sample captured; releasing link")
                box["rotate"] = True
                break
            box["pair_changed"].clear()
            if cfg.max_session_seconds and loop.time() - started >= cfg.max_session_seconds:
                log.info(
                    "rotating BLE link after %.0fs (max_session_seconds)",
                    loop.time() - started,
                )
                box["rotate"] = True
                break
            stop_task = asyncio.create_task(stop.wait())
            change_task = asyncio.create_task(box["pair_changed"].wait())
            try:
                _, pending = await asyncio.wait(
                    {stop_task, change_task},
                    timeout=cfg.poll_interval,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                for task in pending:
                    task.cancel()
        if stop.is_set():
            log.info("session stopped: releasing BLE link")
        elif not box["paired"]:
            log.info("session released: unpaired")
        elif box.get("rotate"):
            log.info("session rotated: handing back for a fresh link")
        else:
            log.info("session ended: BLE link dropped")
        return mqtt
    except BaseException:
        if created and mqtt is not None:
            # The bridge was built (paho loop thread + socket started) but never
            # handed back to run() because the BLE link failed mid-session. If we
            # let it go, its auto-reconnect thread would reconnect forever as the
            # same client id, stealing the session from the *next* bridge and
            # stacking up takeovers. Close it so the thread dies here. (If the
            # failure happened during the sysinfo read, mqtt is still None and
            # there is nothing to close.)
            log.error("session failed before returning; closing new MQTT bridge")
            try:
                mqtt.close()
            except Exception:  # noqa: BLE001 - never mask the original error
                log.exception("failed to close newly-created MQTT bridge")
        raise


async def run(cfg: Config) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass

    pair_changed = asyncio.Event()
    pair_q: asyncio.Queue[bool] = asyncio.Queue()
    cmd_q: asyncio.Queue[tuple[str, bool]] = asyncio.Queue()
    box = {
        "paired": True,
        "polled": False,
        "on_pair": lambda pair: loop.call_soon_threadsafe(pair_q.put_nowait, pair),
        "on_command": lambda key, val: loop.call_soon_threadsafe(
            cmd_q.put_nowait, (key, val)
        ),
        "pair_changed": pair_changed,
    }
    mqtt: MqttBridge | None = None
    client = None
    device = None
    connect_failures = 0
    # address we can always reconnect to: configured one, or the last device we
    # actually discovered (never cleared when `device` is dropped for rediscovery)
    last_address = cfg.controller_address or None
    scan_failures = 0
    fail_streak = 0  # consecutive failed connect cycles; drives the retry backoff

    async def pair_consumer() -> None:
        """Apply pairing commands arriving from the MQTT broker (paho thread).

        State propagation happens BEFORE the BLE teardown and is exception-
        isolated, so a slow/hung disconnect can never block the pairing state
        machine (which would silently swallow later pair commands).
        """
        nonlocal mqtt, client
        while True:
            pair = await pair_q.get()
            if pair == box["paired"]:
                continue
            box["paired"] = pair
            log.info("pairing command: %s", "pair" if pair else "unpair")
            if mqtt is not None:
                try:
                    mqtt.set_availability(pair)
                    mqtt.publish_pairing(pair)
                    mqtt.publish_ble_state("unpaired" if not pair else "connecting")
                except Exception as exc:  # noqa: BLE001 - keep the consumer alive
                    log.error("pairing state publish failed: %s", exc)
            pair_changed.set()
            if not pair:
                await _disconnect_soft(client, cfg.ble_timeout)

    async def command_consumer() -> None:
        """Forward HA switch toggles to controller registers (paho thread)."""
        nonlocal client
        while True:
            key, value = await cmd_q.get()
            reg = registers.SWITCH_REGISTERS.get(key)
            if reg is None or client is None or not client.is_connected:
                log.warning("switch %s: no controller link, dropping write", key)
                continue
            word = 1 if value else 0
            log.info("switch command: %s -> %s (reg %#06x)", key, word, reg)
            try:
                await client.write_holding(reg, word)
            except Exception as exc:  # noqa: BLE001 - don't kill the loop
                log.error("switch %s write failed: %s", key, exc)

    consumer = asyncio.create_task(pair_consumer())
    cmd_consumer = asyncio.create_task(command_consumer())
    try:
        while not stop.is_set():
            if not box["paired"]:
                if mqtt is not None:
                    mqtt.set_availability(False)
                    mqtt.publish_pairing(False)
                    mqtt.publish_ble_state("unpaired")
                log.info("unpaired: holding BLE link off, waiting for a pair command")
                while not stop.is_set() and not box["paired"]:
                    pair_changed.clear()
                    stop_task = asyncio.create_task(stop.wait())
                    pair_task = asyncio.create_task(pair_changed.wait())
                    done, pending = await asyncio.wait(
                        {stop_task, pair_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in pending:
                        task.cancel()
                if box["paired"]:
                    log.info("pair command: reconnecting to controller")
                    device = None
                    connect_failures = 0
                    scan_failures = 0
                continue
            try:
                if cfg.simulate:
                    client = SimulatedModbusClient()
                else:
                    if device is None:
                        # A previous process may have died without disconnecting,
                        # leaving the host BlueZ holding the HCI link. The
                        # controller stops advertising while connected, so a
                        # leftover link makes it undiscoverable forever — release
                        # it before every scan, escalating to a full BlueZ
                        # "remove" after repeated discovery failures.
                        if last_address:
                            # Prefer releasing (Disconnect) over forgetting
                            # (RemoveDevice). Wiping the BlueZ GATT cache forces
                            # a cold service discovery, which the controller's
                            # slow ATT stack (~4 s per answer, ~5 s link timeout)
                            # cannot survive; the vendor app reconnects using its
                            # cached characteristics instead. Only escalate to a
                            # full cache wipe after the device has been invisible
                            # for a very long time (10+ failed scans ~ 3+ min).
                            await release_stale_link(
                                last_address,
                                cfg.ble_adapter,
                                remove=scan_failures >= 10,
                            )
                        device = await discover_controller(
                            cfg.controller_name_prefix,
                            address=cfg.controller_address,
                            adapter=cfg.ble_adapter,
                            scan_timeout=cfg.scan_timeout,
                        )
                        if device is None:
                            raise RuntimeError("no controller found, retrying")
                        last_address = device.address
                        scan_failures = 0
                    client = BleModbusClient(
                        device, cfg.ble_timeout, cfg.read_timeout, pin=cfg.ble_pin
                    )
                if mqtt is not None:
                    mqtt.publish_ble_state("connecting")
                box["polled"] = False
                await client.connect()
                if stop.is_set():
                    log.info("shutdown during connect; releasing BLE link")
                    await _disconnect_soft(client, cfg.ble_timeout)
                    break
                if not box["paired"]:
                    log.info("unpair arrived during connect; releasing link")
                    await _disconnect_soft(client, cfg.ble_timeout)
                    continue
                connect_failures = 0
                mqtt = await session(client, cfg, mqtt, box, stop)
                if stop.is_set():
                    break
                if not box["paired"]:
                    log.info("session released by pairing toggle")
                    continue
                if box.get("rotate"):
                    # Planned end-of-life rotation, not a failure: hand the
                    # link back before the controller's tunnel wedges.
                    # Release FIRST — a dangling idle link for the whole gap
                    # invites the device's supervision timeout and keeps its
                    # single-link slot busy for nothing.
                    box["rotate"] = False
                    fail_streak = 0
                    # Drop the cached BLEDevice: after our disconnect BlueZ
                    # invalidates the object intermittently, and reconnecting
                    # against it fails for minutes with "device not found" /
                    # connect timeouts (the 2026-08-28 dead-zone bug). Always
                    # rediscover; a fresh scan finds the device in one window.
                    device = None
                    await _disconnect_soft(client, cfg.ble_timeout)
                    await asyncio.sleep(cfg.rotate_gap_seconds)
                    continue
                # poll loop broke while still paired -> the BLE link dropped
                connect_failures += 1
                log.error("session failed: BLE link dropped")
                if mqtt is not None:
                    # Deliberately no set_availability(False) here: a BLE
                    # hiccup must not flip every HA entity to unavailable
                    # (entities keep their last retained values instead).
                    # Link state stays visible via the ble_pairing_state
                    # diagnostic entity; availability now only tracks the
                    # bridge process (birth/LWT/graceful close).
                    mqtt.publish_ble_state("disconnected")
                if connect_failures >= 3:
                    log.info("rediscovering controller after %d failures", connect_failures)
                    device = None
                    connect_failures = 0
                fail_streak = 0 if box["polled"] else fail_streak + 1
                wait = _retry_backoff(cfg.retry_interval, fail_streak, cfg.retry_backoff_max)
                log.info("retrying in %.0fs (consecutive failures: %d)", wait, fail_streak)
                try:
                    await asyncio.wait_for(stop.wait(), wait)
                except asyncio.TimeoutError:
                    pass
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep the bridge alive
                connect_failures += 1
                log.error("session failed: %s", exc)
                if mqtt is not None:
                    # No set_availability(False): see the BLE-dropped branch.
                    mqtt.publish_ble_state("disconnected")
                if not box["paired"]:
                    continue
                if connect_failures >= 3:
                    log.info("rediscovering controller after %d failures", connect_failures)
                    had_device = device is not None
                    device = None
                    connect_failures = 0
                    if had_device and last_address:
                        # The controller was found but keeps dropping during/after
                        # connect. The vendor app reconnects against its cached
                        # GATT characteristics (the controller's ATT answers are
                        # slow and it drops the link ~5 s after connect), so we
                        # must NOT wipe the cache here: a cold service discovery
                        # is exactly what the controller cannot survive. Just
                        # release any stale link and let the next connect reuse
                        # the warm cache.
                        await release_stale_link(last_address, cfg.ble_adapter)
                if device is None:
                    scan_failures += 1
                    if scan_failures >= 6 and scan_failures % 6 == 0:
                        log.warning(
                            "controller %s still not advertising after %d scans; "
                            "if it keeps hiding, power-cycle it (it stops "
                            "advertising while it holds a stale BLE link)",
                            last_address, scan_failures,
                        )
                fail_streak = 0 if box["polled"] else fail_streak + 1
                # Only a real Modbus exception response proves the controller
                # is awake with its rate limiter armed - those earn the long
                # backoff. Silence (timeouts, stalled connects, missing
                # adverts) means it is merely deaf/asleep: harmless to probe
                # again quickly, and its healthy phases can return within
                # minutes.
                awake_and_gated = isinstance(exc, modbus.ModbusError)
                if awake_and_gated:
                    wait = _retry_backoff(cfg.retry_interval, fail_streak, cfg.retry_backoff_max)
                else:
                    wait = cfg.scan_retry_interval
                log.info("retrying in %.0fs (consecutive failures: %d)", wait, fail_streak)
                try:
                    await asyncio.wait_for(stop.wait(), wait)
                except asyncio.TimeoutError:
                    pass
    finally:
        consumer.cancel()
        cmd_consumer.cancel()
        try:
            await consumer
            await cmd_consumer
        except asyncio.CancelledError:
            pass
        if client is not None:
            await _disconnect_soft(client, cfg.ble_timeout)
        if mqtt is not None:
            mqtt.close()
        log.info("shutdown complete")


def main() -> None:
    cfg = Config.from_env()
    logging.basicConfig(
        level=cfg.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    log.info("starting limu-solar PoC (simulate=%s)", cfg.simulate)
    asyncio.run(run(cfg))


if __name__ == "__main__":
    main()
