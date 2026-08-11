"""Main loop: discover controller -> poll telemetry over BLE -> publish to MQTT.

    LTM-252245 (BLE GATT FFF1)  --Modbus RTU-->  this bridge  --MQTT-->  Home Assistant
"""
from __future__ import annotations

import asyncio
import logging
import signal

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


async def _log_stats_diagnostic(client) -> None:
    """One-shot INFO log of the raw statistics registers (0x0300-0x0313) so the
    device's exact output can be cross-checked against the decoded entities
    (e.g. full-charge count at 0x0306, over-discharge count at 0x0307)."""
    try:
        raw = await client.read_holding(*registers.BLOCK_STATS)
    except Exception as exc:  # noqa: BLE001 - optional diagnostic
        log.info("stats diagnostic: block read failed: %s", exc)
        return
    base = registers.BLOCK_STATS[0]
    log.info(
        "stats raw: %s",
        ", ".join(f"{base + i:#06x}={v:#06x}({v})" for i, v in enumerate(raw)),
    )


async def poll_once(client) -> dict:
    """Read all verified public blocks and decode them into one state dict.

    The extension/stats blocks are optional telemetry: if the controller fails
    one of those reads, keep the poll alive with what we got rather than
    dropping the whole BLE link (a single failure shouldn't kill a session).
    """
    running = await client.read_holding(*registers.BLOCK_RUNNING)
    connect = await client.read_holding(*registers.BLOCK_CONNECT)
    state = {
        **registers.decode_running(running),
        **registers.decode_connect(connect),
    }
    for block, decoder in (
        (registers.BLOCK_EXT, registers.decode_extension),
        (registers.BLOCK_STATS, registers.decode_stats),
    ):
        try:
            regs = await client.read_holding(*block)
            state.update(decoder(regs))
        except Exception as exc:  # noqa: BLE001 - optional block, keep polling
            log.warning("optional block %#06x failed: %s", block[0], exc)
    return state


async def session(
    client, cfg: Config, mqtt: MqttBridge | None, box: dict, stop: asyncio.Event
) -> MqttBridge:
    """One paired session: sysinfo + discovery, then poll until an unpair
    command arrives, the BLE link drops, or shutdown is requested. Returns the
    (possibly new) bridge. `stop` is observed so SIGTERM unwinds to a clean
    `client.disconnect()` instead of leaving the BLE link held."""
    if mqtt is None:
        sysinfo = registers.decode_sysinfo(
            await client.read_holding(*registers.BLOCK_SYSINFO)
        )
        node_id = sysinfo.get("serial_number") or client.name.replace(" ", "_")
        log.info(
            "controller: %s %s (SN %s, sw %s)",
            sysinfo.get("manufacturer"), sysinfo.get("model"),
            node_id, sysinfo.get("sw_version"),
        )
        mqtt = MqttBridge(
            cfg, node_id, sysinfo,
            on_pair=box["on_pair"], on_command=box["on_command"],
        )
        mqtt.connect()
        mqtt.publish_discovery()
    mqtt.set_availability(True)
    mqtt.publish_pairing(True)
    mqtt.publish_ble_state("connected")
    await _log_stats_diagnostic(client)

    while box["paired"] and client.is_connected and not stop.is_set():
        state = await poll_once(client)
        log.info(
            "PV %.2f V / %.1f W | batt %.2f V / %.1f%% | charge %s %.1f W | %s",
            state["pv_voltage"], state["pv_power"],
            state["battery_voltage"], state["battery_soc"],
            state["charge_phase"], state["charge_power"], state["running_state"],
        )
        mqtt.publish_state(state)
        box["pair_changed"].clear()
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
    elif box["paired"]:
        log.info("session ended: BLE link dropped")
    else:
        log.info("session released: unpaired")
    return mqtt


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
                            await release_stale_link(
                                last_address,
                                cfg.ble_adapter,
                                remove=scan_failures >= 3,
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
                # poll loop broke while still paired -> the BLE link dropped
                connect_failures += 1
                log.error("session failed: BLE link dropped")
                if mqtt is not None:
                    mqtt.set_availability(False)
                    mqtt.publish_ble_state("disconnected")
                if connect_failures >= 3:
                    log.info("rediscovering controller after %d failures", connect_failures)
                    device = None
                    connect_failures = 0
                try:
                    await asyncio.wait_for(stop.wait(), cfg.retry_interval)
                except asyncio.TimeoutError:
                    pass
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep the bridge alive
                connect_failures += 1
                log.error("session failed: %s", exc)
                if mqtt is not None:
                    mqtt.set_availability(False)
                    mqtt.publish_ble_state("disconnected")
                if not box["paired"]:
                    continue
                if connect_failures >= 3:
                    log.info("rediscovering controller after %d failures", connect_failures)
                    device = None
                    connect_failures = 0
                if device is None:
                    scan_failures += 1
                    if scan_failures >= 6 and scan_failures % 6 == 0:
                        log.warning(
                            "controller %s still not advertising after %d scans; "
                            "if it keeps hiding, power-cycle it (it stops "
                            "advertising while it holds a stale BLE link)",
                            last_address, scan_failures,
                        )
                try:
                    await asyncio.wait_for(stop.wait(), cfg.retry_interval)
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
