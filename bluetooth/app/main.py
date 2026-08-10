"""Main loop: discover controller -> poll telemetry over BLE -> publish to MQTT.

    LTM-252245 (BLE GATT FFF1)  --Modbus RTU-->  this bridge  --MQTT-->  Home Assistant
"""
from __future__ import annotations

import asyncio
import logging
import signal

from . import registers
from .ble_client import BleModbusClient, SimulatedModbusClient, discover_controller
from .config import Config
from .mqtt_client import MqttBridge

log = logging.getLogger(__name__)


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


async def session(client, cfg: Config, mqtt: MqttBridge | None, box: dict) -> MqttBridge:
    """One paired session: sysinfo + discovery, then poll until an unpair
    command arrives or the BLE link drops. Returns the (possibly new) bridge."""
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
        mqtt = MqttBridge(cfg, node_id, sysinfo, on_pair=box["on_pair"])
        mqtt.connect()
        mqtt.publish_discovery()
    mqtt.set_availability(True)
    mqtt.publish_pairing(True)

    while box["paired"] and client.is_connected:
        state = await poll_once(client)
        log.info(
            "PV %.2f V / %.1f W | batt %.2f V / %.1f%% | charge %s %.1f W | %s",
            state["pv_voltage"], state["pv_power"],
            state["battery_voltage"], state["battery_soc"],
            state["charge_phase"], state["charge_power"], state["running_state"],
        )
        mqtt.publish_state(state)
        box["pair_changed"].clear()
        try:
            await asyncio.wait_for(box["pair_changed"].wait(), cfg.poll_interval)
        except asyncio.TimeoutError:
            pass
    if box["paired"]:
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
    box = {
        "paired": True,
        "on_pair": lambda pair: loop.call_soon_threadsafe(pair_q.put_nowait, pair),
        "pair_changed": pair_changed,
    }
    mqtt: MqttBridge | None = None
    client = None
    device = None
    connect_failures = 0

    async def pair_consumer() -> None:
        """Apply pairing commands arriving from the MQTT broker (paho thread)."""
        nonlocal mqtt, client
        while True:
            pair = await pair_q.get()
            if pair == box["paired"]:
                continue
            box["paired"] = pair
            log.info("pairing command: %s", "pair" if pair else "unpair")
            if not pair and client is not None:
                await client.disconnect()
            if mqtt is not None:
                mqtt.set_availability(pair)
                mqtt.publish_pairing(pair)
            pair_changed.set()

    consumer = asyncio.create_task(pair_consumer())
    try:
        while not stop.is_set():
            if not box["paired"]:
                if mqtt is not None:
                    mqtt.set_availability(False)
                    mqtt.publish_pairing(False)
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
                continue
            try:
                if cfg.simulate:
                    client = SimulatedModbusClient()
                else:
                    if device is None:
                        device = await discover_controller(
                            cfg.controller_name_prefix,
                            address=cfg.controller_address,
                            adapter=cfg.ble_adapter,
                            scan_timeout=cfg.scan_timeout,
                        )
                        if device is None:
                            raise RuntimeError("no controller found, retrying")
                    client = BleModbusClient(
                        device, cfg.ble_timeout, cfg.read_timeout, pin=cfg.ble_pin
                    )
                await client.connect()
                if not box["paired"]:
                    log.info("unpair arrived during connect; releasing link")
                    await client.disconnect()
                    continue
                connect_failures = 0
                mqtt = await session(client, cfg, mqtt, box)
                if not box["paired"]:
                    log.info("session released by pairing toggle")
                    continue
                # poll loop broke while still paired -> the BLE link dropped
                connect_failures += 1
                log.error("session failed: BLE link dropped")
                if mqtt is not None:
                    mqtt.set_availability(False)
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
                if not box["paired"]:
                    continue
                if connect_failures >= 3:
                    log.info("rediscovering controller after %d failures", connect_failures)
                    device = None
                    connect_failures = 0
                try:
                    await asyncio.wait_for(stop.wait(), cfg.retry_interval)
                except asyncio.TimeoutError:
                    pass
    finally:
        consumer.cancel()
        try:
            await consumer
        except asyncio.CancelledError:
            pass
        if client is not None:
            await client.disconnect()
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
