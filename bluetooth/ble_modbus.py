#!/usr/bin/env python3
"""BLE Modbus tool for Noi Solar LTM controller (LTM-252245).

Transport: BLE service FFF1, write Modbus RTU frames to BBB1 (writeWithoutResponse),
responses arrive on AAA1 (notify).

Usage:
  ble_modbus.py scan <start> <end> [chunk]     scan holding registers with FC03
  ble_modbus.py read <reg> <qty>               single FC03 read
  ble_modbus.py sniff                          just connect and dump all notifications
"""
import sys, time
import objc
from Foundation import NSObject, NSRunLoop, NSDate
from CoreBluetooth import CBCentralManager, CBManagerStatePoweredOn

TARGET  = 'LTM-252245'
TARGET_UUID = '1FBE6253-5026-BD17-135C-F937BEEB5992'  # CoreBluetooth id, stable on this Mac
SERVICE = '0000FFF1-0000-0000-0000-000000000000'
NOTIFY  = '0000AAA1-0000-0000-0000-000000000000'
WRITE   = '0000BBB1-0000-0000-0000-000000000000'


def crc16(data):
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def read_frame(start_reg, qty, addr=0xFF, fc=0x03):
    body = bytes([addr, fc, (start_reg >> 8) & 0xFF, start_reg & 0xFF,
                  (qty >> 8) & 0xFF, qty & 0xFF])
    c = crc16(body)
    # device firmware uses BIG-endian CRC (confirmed via syncTime frame FF 10 05 04 00 04 19 95)
    return body + bytes([(c >> 8) & 0xFF, c & 0xFF])


def write_multi_frame(start_reg, values, addr=0xFF):
    """FC 0x10 write multiple registers. values = list of 16-bit ints."""
    qty = len(values)
    data = b''.join(bytes([(v >> 8) & 0xFF, v & 0xFF]) for v in values)
    body = bytes([addr, 0x10, (start_reg >> 8) & 0xFF, start_reg & 0xFF,
                  (qty >> 8) & 0xFF, qty & 0xFF, len(data)]) + data
    c = crc16(body)
    return body + bytes([(c >> 8) & 0xFF, c & 0xFF])


def ascii_regs(s):
    """Pack an ASCII string into 16-bit registers (2 chars per reg)."""
    if len(s) % 2:
        s += '\x00'
    return [(ord(s[i]) << 8) | ord(s[i + 1]) for i in range(0, len(s), 2)]


class Client(NSObject):
    def init(self):
        self = objc.super(Client, self).init()
        if self is None:
            return None
        self.central = None
        self.p = None
        self.wc = None
        self.buf = bytearray()
        self.connected = False
        self.ready = False
        self.rx_event = False
        return self

    # ---- central callbacks ----
    def centralManagerDidUpdateState_(self, cent):
        log(f'central state {cent.state()}')
        if cent.state() == CBManagerStatePoweredOn:
            self.start_scan()

    def start_scan(self):
        # prefer direct retrieve by known UUID (works even if adv packets are missed)
        if TARGET_UUID:
            from CoreBluetooth import CBUUID as _CU
            from Foundation import NSUUID
            res = self.central.retrievePeripheralsWithIdentifiers_(
                [NSUUID.alloc().initWithUUIDString_(TARGET_UUID)])
            if res:
                p = res[0]
                log(f'retrieved known peripheral {p.name()} state={p.state()}')
                self.p = p
                self.central.connectPeripheral_options_(p, None)
                return
        self.central.scanForPeripheralsWithServices_options_(
            None, {'CBCentralManagerScanOptionAllowDuplicatesKey': True})
        log('scanning...')

    def centralManager_didDiscoverPeripheral_advertisementData_RSSI_(self, cent, p, a, r):
        nm = (p.name() or '').strip()
        if nm:
            log(f'see: {nm!r} rssi={int(r)}')
        if nm == TARGET and self.p is None:
            log(f'found {TARGET} rssi={int(r)}')
            self.p = p
            cent.stopScan()
            cent.connectPeripheral_options_(p, None)

    def centralManager_didConnectPeripheral_(self, cent, p):
        log('CONNECTED')
        self.connected = True
        p.setDelegate_(self)
        p.discoverServices_(None)

    def centralManager_didFailToConnectPeripheral_error_(self, cent, p, err):
        log(f'connect FAIL: {err}')
        self.p = None
        self.start_scan()

    def centralManager_didDisconnectPeripheral_error_(self, cent, p, err):
        log(f'DISCONNECTED err={err}')
        self.connected = False
        self.ready = False

    # ---- peripheral callbacks ----
    def peripheral_didDiscoverServices_(self, p, err):
        for s in p.services() or []:
            if str(s.UUID()).upper() == SERVICE.upper():
                p.discoverCharacteristics_forService_(None, s)

    def peripheral_didDiscoverCharacteristicsForService_error_(self, p, s, err):
        for c in s.characteristics():
            u = str(c.UUID()).upper()
            if u == NOTIFY.upper():
                p.setNotifyValue_forCharacteristic_(True, c)
            elif u == WRITE.upper():
                self.wc = c

    def peripheral_didUpdateNotificationStateForCharacteristic_error_(self, p, c, err):
        if err is None and self.wc is not None:
            log('notify subscribed, ready')
            self.ready = True
        else:
            log(f'notify-state err={err}')

    def peripheral_didUpdateValueForCharacteristic_error_(self, p, c, err):
        v = c.value()
        if v:
            self.buf.extend(bytes(v))
            self.rx_event = True


def log(msg):
    print(f'[{time.strftime("%H:%M:%S")}] {msg}', flush=True)


def pump(seconds):
    end = time.time() + seconds
    while time.time() < end:
        NSRunLoop.currentRunLoop().runUntilDate_(
            NSDate.dateWithTimeIntervalSinceNow_(0.1))


def wait_for(pred, timeout, desc):
    end = time.time() + timeout
    while time.time() < end:
        NSRunLoop.currentRunLoop().runUntilDate_(
            NSDate.dateWithTimeIntervalSinceNow_(0.1))
        if pred():
            return True
    log(f'timeout waiting for {desc}')
    return False


def expected_len(buf):
    if len(buf) < 3:
        return None
    fc = buf[1]
    if fc & 0x80:          # exception: addr fc excode crc
        return 5
    if fc == 0x03:
        return 3 + buf[2] + 2
    if fc in (0x06, 0x10):  # write ack: addr fc reg(2) qty/val(2) crc(2)
        return 8
    return None


def do_read(cli, reg, qty, timeout=2.5):
    cli.buf.clear()
    cli.rx_event = False
    f = read_frame(reg, qty)
    cli.p.writeValue_forCharacteristic_type_(f, cli.wc, 1)
    end = time.time() + timeout
    while time.time() < end:
        NSRunLoop.currentRunLoop().runUntilDate_(
            NSDate.dateWithTimeIntervalSinceNow_(0.1))
        exp = expected_len(cli.buf)
        if exp is not None and len(cli.buf) >= exp:
            break
    raw = bytes(cli.buf)
    if not raw:
        return None
    return raw


def write_single_frame(reg, value, addr=0xFF):
    """FC 0x06 write single register."""
    body = bytes([addr, 0x06, (reg >> 8) & 0xFF, reg & 0xFF, (value >> 8) & 0xFF, value & 0xFF])
    c = crc16(body)
    return body + bytes([(c >> 8) & 0xFF, c & 0xFF])


def do_write_single(cli, reg, value, timeout=1.5):
    cli.buf.clear(); cli.rx_event = False
    f = write_single_frame(reg, value)
    cli.p.writeValue_forCharacteristic_type_(f, cli.wc, 1)
    end = time.time() + timeout
    while time.time() < end:
        pump(0.05)
        exp = expected_len(cli.buf)
        if exp is not None and len(cli.buf) >= exp:
            break
    return bytes(cli.buf) if cli.buf else None


def do_write_multi(cli, reg, values, timeout=2.5):
    cli.buf.clear(); cli.rx_event = False
    f = write_multi_frame(reg, values)
    log(f'>>> write 0x10 reg={reg:#06x} values={[hex(v) for v in values]} frame={f.hex()}')
    cli.p.writeValue_forCharacteristic_type_(f, cli.wc, 1)
    end = time.time() + timeout
    while time.time() < end:
        pump(0.1)
        exp = expected_len(cli.buf)
        if exp is not None and len(cli.buf) >= exp:
            break
    return bytes(cli.buf) if cli.buf else None


def decode(raw):
    """Return human string for a response frame."""
    if raw is None:
        return 'TIMEOUT'
    if len(raw) >= 3 and raw[1] & 0x80:
        return f'EXCEPTION fc={raw[1]&0x7f:#04x} code={raw[2]:#04x} raw={raw.hex()}'
    if len(raw) >= 5 and raw[1] == 0x03:
        n = raw[2]
        data = raw[3:3 + n]
        regs = [int.from_bytes(data[i:i + 2], 'big') for i in range(0, len(data) - 1, 2)]
        ascii_s = ''.join(chr(b) if 32 <= b < 127 else '.' for b in data)
        return f'regs={regs} ascii={ascii_s!r} raw={raw.hex()}'
    return f'raw={raw.hex()}'


def ensure_connected(cli):
    """Block until connected + notify ready. Auto-rescans on failure."""
    t0 = time.time()
    while True:
        pump(0.5)
        if cli.ready:
            return True
        if cli.connected:
            continue
        if time.time() - t0 > 12:
            log('connect watchdog: retrying scan')
            if cli.p is not None:
                try:
                    cli.central.cancelPeripheralConnection_(cli.p)
                except Exception:
                    pass
                cli.p = None
            cli.start_scan()
            t0 = time.time()


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else 'scan'
    cli = Client.alloc().init()
    cli.central = CBCentralManager.alloc().initWithDelegate_queue_(cli, None)

    ensure_connected(cli)

    if mode == 'sniff':
        log('sniffing 20s...')
        pump(20)
        log(f'RX: {bytes(cli.buf).hex() if cli.buf else "(none)"}')
        return 0

    if mode == 'read':
        reg = int(sys.argv[2], 0)
        qty = int(sys.argv[3], 0)
        log(f'read reg={reg:#06x} qty={qty}')
        print(decode(do_read(cli, reg, qty)))
        return 0

    if mode == 'idle':
        log('idling 8s')
        pump(8)
        log(f'idle done. connected={cli.connected} ready={cli.ready}')
        return 0

    if mode == 'auth2':
        # try PIN 666666 in multiple formats/registers; probe gate after each
        combos = []
        for r in (0x0400, 0x0500):
            combos.append(('0x10-ascii', r, ascii_regs('666666')))
            combos.append(('0x10-zeros', r, [0, 0, 0]))
        for r in (0x0400, 0x0500, 0x0508):
            combos.append(('0x06-zero', r, 0))
        unlocked = False
        for kind, reg, payload in combos:
            if not cli.ready:
                log('link lost, reconnecting...')
                cli.p = None
                ensure_connected(cli)
            if kind.startswith('0x10'):
                raw = do_write_multi(cli, reg, payload)
            else:
                raw = do_write_single(cli, reg, payload)
            print(f'{kind} @ {reg:#06x}: {decode(raw)}', flush=True)
            pump(0.15)
            probe = do_read(cli, 0x0400, 1)
            print(f'   probe 0x0400: {decode(probe)}', flush=True)
            if probe and len(probe) >= 5 and probe[1] == 0x03:
                log(f'*** GATE OPENED by {kind} @ {reg:#06x} ***')
                unlocked = True
                break
            pump(0.1)
        if not unlocked:
            log('no combo unlocked the gate')
        return 0

    if mode == 'auth_probe':
        # probe candidate registers with FC 0x06 to find password register (looking for code=0x08)
        for r in range(0x03F0, 0x0530):
            if not cli.ready:
                log('link lost, reconnecting...')
                cli.p = None
                ensure_connected(cli)
            raw = do_write_single(cli, r, 0x0001)
            print(f'{r:#06x}: {decode(raw)}', flush=True)
            pump(0.05)
            if raw and len(raw) >= 3 and raw[1] == 0x86 and raw[2] == 0x08:
                log(f'FOUND password register at {r:#06x}!')
                break
        return 0

    if mode == 'auth':
        # auth <pin> <reg>: write ASCII PIN to candidate password register, then probe
        pin = sys.argv[2]
        regs = [int(x, 0) for x in sys.argv[3].split(',')] if len(sys.argv) > 3 else [0x0400, 0x0500]
        vals = ascii_regs(pin)
        for r in regs:
            raw = do_write_multi(cli, r, vals)
            print(f'auth-write {r:#06x}: {decode(raw)}', flush=True)
            pump(0.2)
            # probe: did the gated area open up?
            probe = do_read(cli, 0x0400, 0x08)
            print(f'probe 0x0400: {decode(probe)}', flush=True)
            pump(0.2)
        return 0

    if mode == 'multi':
        # comma-separated reg:qty pairs, e.g. 0x000A:1,0x000B:6
        for pair in sys.argv[2].split(','):
            reg_s, qty_s = pair.split(':')
            reg, qty = int(reg_s, 0), int(qty_s, 0)
            raw = do_read(cli, reg, qty)
            print(f'{reg:#06x}: {decode(raw)}', flush=True)
            pump(0.1)
        return 0

    # scan mode
    start = int(sys.argv[2], 0) if len(sys.argv) > 2 else 0x0500
    endr = int(sys.argv[3], 0) if len(sys.argv) > 3 else 0x0600
    chunk = int(sys.argv[4], 0) if len(sys.argv) > 4 else 0x10
    log(f'scanning {start:#06x}..{endr:#06x} chunk={chunk:#x}')
    reg = start
    while reg < endr:
        if not cli.ready:
            log('link lost, reconnecting...')
            cli.p = None
            ensure_connected(cli)
        qty = min(chunk, endr - reg)
        raw = do_read(cli, reg, qty)
        print(f'{reg:#06x}: {decode(raw)}', flush=True)
        reg += qty
        pump(0.15)
    return 0


if __name__ == '__main__':
    sys.exit(main())
