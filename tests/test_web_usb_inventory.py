from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.config import VMConfig
from core.web_agent import WebAgentService
from core.web_rpc import RpcRemoteError
from core.vm_lifecycle import _default_usb_auto_attach
from web.api.contracts import VmConfigDraft, VmProvisionRequest


class WebUsbInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def service(self, devices):
        return WebAgentService(
            Path(self.temp.name) / "vms.json",
            usb_inventory=lambda: devices,
        )

    @staticmethod
    def device(vendor: str, product: str, label: str, serial: str = "private-serial"):
        return SimpleNamespace(
            vendor_id=vendor, product_id=product, product=label,
            manufacturer="private manufacturer", serial=serial,
            sysfs="/sys/private/path", bus="3", addr="4",
        )

    def test_inventory_groups_duplicate_ids_and_does_not_expose_host_identifiers(self):
        service = self.service([
            self.device("1234", "abcd", "Teclado"),
            self.device("1234", "abcd", "Teclado reserva", "other-secret"),
            self.device("9876", "0001", "Mouse\nHost"),
        ])

        result = service.host_usb_inventory({})

        self.assertEqual(len(result["items"]), 2)
        duplicate = next(item for item in result["items"] if item["device_id"] == "1234:abcd")
        self.assertEqual(duplicate["device_count"], 2)
        self.assertFalse(duplicate["auto_attach_supported"])
        unique = next(item for item in result["items"] if item["device_id"] == "9876:0001")
        self.assertTrue(unique["auto_attach_supported"])
        serialized = repr(result)
        self.assertNotIn("private-serial", serialized)
        self.assertNotIn("/sys/private/path", serialized)
        self.assertNotIn("private manufacturer", serialized)

    def test_only_new_unique_current_devices_can_be_selected(self):
        service = self.service([
            self.device("1234", "abcd", "Unique"),
            self.device("1111", "2222", "Duplicate one"),
            self.device("1111", "2222", "Duplicate two"),
        ])
        existing = VMConfig(id="vm", name="VM", macos_version="tahoe", usb_auto_attach=["dead:beef"])

        self.assertEqual(service._validated_usb_auto_attach(["dead:beef"], existing=existing), ["dead:beef"])
        self.assertEqual(service._validated_usb_auto_attach(["1234:ABCD"], existing=existing), ["1234:abcd"])
        with self.assertRaises(RpcRemoteError) as caught:
            service._validated_usb_auto_attach(["1111:2222"])
        self.assertEqual(caught.exception.code, "usb_selection_unavailable")
        with self.assertRaises(RpcRemoteError):
            service._validated_usb_auto_attach(["9999:9999"])

    def test_unavailable_inventory_blocks_new_selection_but_preserves_unchanged_saved_value(self):
        service = self.service(None)
        service.usb_inventory = lambda: (_ for _ in ()).throw(OSError("sysfs unavailable"))
        existing = VMConfig(id="vm", name="VM", macos_version="tahoe", usb_auto_attach=["dead:beef"])

        self.assertEqual(service._validated_usb_auto_attach(["dead:beef"], existing=existing), ["dead:beef"])
        with self.assertRaises(RpcRemoteError) as caught:
            service._validated_usb_auto_attach(["1234:abcd"])
        self.assertEqual(caught.exception.code, "usb_selection_unavailable")

    def test_contracts_normalize_and_reject_ambiguous_selection_lists(self):
        draft = VmConfigDraft(
            name="VM", macos_version="tahoe", vcpus=4, ram_gb=8,
            disk_size_gb=64, usb_auto_attach=["1234:ABCD"],
        )
        self.assertEqual(draft.usb_auto_attach, ["1234:abcd"])
        with self.assertRaises(ValueError):
            VmProvisionRequest(
                idempotency_key="request-123", id="vm", name="VM", macos_version="tahoe",
                vcpus=4, ram_gb=8, installer_policy="none",
                usb_auto_attach=["1234:abcd", "1234:ABCD"],
            )

    def test_boot_auto_attach_skips_ids_that_became_ambiguous(self):
        vm = VMConfig(
            id="vm", name="VM", macos_version="tahoe",
            usb_auto_attach=["1234:abcd", "9876:0001"],
        )
        duplicate = [self.device("1234", "abcd", "One"), self.device("1234", "abcd", "Two")]
        unique = self.device("9876", "0001", "Unique")
        with patch("core.reims_ctl.qmp_socket", return_value="/tmp/vm.qmp"), \
             patch("core.passthrough.list_usb_devices", return_value=[*duplicate, unique]), \
             patch("core.passthrough.attach") as attach:
            _default_usb_auto_attach(vm)
        attach.assert_called_once_with("/tmp/vm.qmp", unique)


if __name__ == "__main__":
    unittest.main()
