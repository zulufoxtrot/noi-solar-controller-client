"""Unit tests for `release_stale_link` (BlueZ stale-link recovery).

The helper lazy-imports the BlueZ/D-Bus modules inside its body, so tests can
inject fake `bleak.backends.bluezdbus` / `dbus_fast` modules into `sys.modules`
on any platform and assert the exact D-Bus calls it would make on Linux.
"""
import sys
import types
import unittest
from unittest import mock

from bluetooth.app.ble_client import _bluez_adapter, release_stale_link
from bluetooth.app.main import _bluez_hint

ADDR = "B4:C2:E0:E0:50:BC"
ADAPTER_PATH = "/org/bluez/hci0"
DEV_PATH = "/org/bluez/hci0/dev_B4_C2_E0_E0_50_BC"

_INJECTED_KEYS = (
    "dbus_fast",
    "bleak.backends.bluezdbus",
    "bleak.backends.bluezdbus.defs",
    "bleak.backends.bluezdbus.manager",
)


class FakeMessage:
    def __init__(
        self,
        destination=None,
        interface=None,
        path=None,
        member=None,
        signature=None,
        body=None,
    ):
        self.destination = destination
        self.interface = interface
        self.path = path
        self.member = member
        self.signature = signature
        self.body = body


def inject_bluez_fakes(calls, connected=True):
    """Put fake BlueZ/D-Bus modules in sys.modules; each D-Bus call is appended
    to `calls` as a FakeMessage."""
    dbus = types.ModuleType("dbus_fast")
    dbus.Message = FakeMessage

    defs = types.ModuleType("bleak.backends.bluezdbus.defs")
    defs.BLUEZ_SERVICE = "org.bluez"
    defs.DEVICE_INTERFACE = "org.bluez.Device1"
    defs.ADAPTER_INTERFACE = "org.bluez.Adapter1"

    bluezdbus = types.ModuleType("bleak.backends.bluezdbus")
    bluezdbus.defs = defs

    manager = types.ModuleType("bleak.backends.bluezdbus.manager")

    async def get_global_bluez_manager():
        bus = mock.Mock()

        async def call(msg):
            calls.append(msg)

        bus.call = call
        mgr = mock.Mock()
        mgr.get_default_adapter.return_value = ADAPTER_PATH
        mgr.is_connected.side_effect = lambda p: (
            connected and p.endswith(f"dev_{ADDR.replace(':', '_')}")
        )
        mgr._bus = bus
        return mgr

    manager.get_global_bluez_manager = get_global_bluez_manager

    sys.modules["dbus_fast"] = dbus
    sys.modules["bleak.backends.bluezdbus"] = bluezdbus
    sys.modules["bleak.backends.bluezdbus.defs"] = defs
    sys.modules["bleak.backends.bluezdbus.manager"] = manager


class ReleaseStaleLinkTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._saved = {k: sys.modules.get(k) for k in _INJECTED_KEYS}

    def tearDown(self):
        for k in _INJECTED_KEYS:
            if self._saved[k] is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = self._saved[k]

    async def test_disconnects_when_bluez_holds_link(self):
        calls = []
        inject_bluez_fakes(calls, connected=True)
        self.assertTrue(await release_stale_link(ADDR))
        self.assertEqual(len(calls), 1)
        msg = calls[0]
        self.assertEqual(msg.interface, "org.bluez.Device1")
        self.assertEqual(msg.member, "Disconnect")
        self.assertEqual(msg.path, DEV_PATH)
        self.assertIsNone(msg.body)

    async def test_no_call_when_not_connected(self):
        calls = []
        inject_bluez_fakes(calls, connected=False)
        self.assertFalse(await release_stale_link(ADDR))
        self.assertEqual(calls, [])

    async def test_uses_explicit_adapter(self):
        calls = []
        inject_bluez_fakes(calls, connected=True)
        self.assertTrue(await release_stale_link(ADDR, adapter="hci2"))
        self.assertEqual(calls[0].path, "/org/bluez/hci2/dev_B4_C2_E0_E0_50_BC")

    async def test_remove_device_escalation(self):
        calls = []
        inject_bluez_fakes(calls, connected=True)
        self.assertTrue(await release_stale_link(ADDR, remove=True))
        self.assertEqual(len(calls), 1)
        msg = calls[0]
        self.assertEqual(msg.interface, "org.bluez.Adapter1")
        self.assertEqual(msg.member, "RemoveDevice")
        self.assertEqual(msg.signature, "o")
        self.assertEqual(msg.body, [DEV_PATH])

    async def test_noop_without_address(self):
        calls = []
        inject_bluez_fakes(calls, connected=True)
        self.assertFalse(await release_stale_link(""))
        self.assertEqual(calls, [])

    async def test_noop_when_bluez_backend_import_fails(self):
        # simulate a platform/install where the BlueZ backend cannot be imported
        broken = types.ModuleType("bleak.backends.bluezdbus.manager")

        def _missing(name):
            raise ImportError(f"no {name}")

        broken.__getattr__ = _missing
        sys.modules["bleak.backends.bluezdbus.manager"] = broken
        self.assertFalse(await release_stale_link(ADDR))

    def test_bluez_adapter_parses_dbus_path(self):
        class Dev1:
            details = {"path": "/org/bluez/hci1/dev_XX"}

        class Dev2:
            details = {"identifier": "some-uuid"}

        self.assertEqual(_bluez_adapter(Dev1()), "hci1")
        self.assertEqual(_bluez_adapter(Dev2()), "")


class BluezHintTest(unittest.TestCase):
    def test_dbus_activation_timeout(self):
        exc = Exception(
            "[org.freedesktop.DBus.Error.TimedOut] "
            "Failed to activate service 'org.bluez': timed out "
            "(service_start_timeout=25000ms)"
        )
        self.assertIsNotNone(_bluez_hint(exc))

    def test_missing_adapter(self):
        self.assertIsNotNone(_bluez_hint(Exception("adapter 'hci0' not found")))

    def test_service_unknown(self):
        self.assertIsNotNone(_bluez_hint(Exception("org.freedesktop.DBus.Error.ServiceUnknown")))

    def test_controller_not_found_is_not_bluez(self):
        self.assertIsNone(_bluez_hint(Exception("no controller found, retrying")))

    def test_modbus_read_timeout_is_not_bluez(self):
        self.assertIsNone(_bluez_hint(Exception("request timed out after 5s")))

    def test_hint_mentions_host_checks(self):
        hint = _bluez_hint(Exception("adapter 'hci0' not found"))
        self.assertIn("systemctl status bluetooth", hint)
        self.assertIn("hciconfig hci0", hint)


if __name__ == "__main__":
    unittest.main()
