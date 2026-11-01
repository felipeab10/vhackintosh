from __future__ import annotations

import tempfile
import unittest
import json
import os
import errno
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from core.config import ExclusiveVMLock, VMConfig


class VMResourceAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.reservations = root / "reservations"
        self.lock_file = root / "reservations.lock"
        self.marker = root / "maintenance.json"
        self.patches = [
            patch("core.config.VM_RESERVATIONS_DIR", self.reservations),
            patch("core.config.VM_RESERVATIONS_LOCK", self.lock_file),
            patch("core.config.VM_MAINTENANCE_MARKER", self.marker),
            patch.object(ExclusiveVMLock, "_running_reims_vms", return_value=({}, True)),
            patch.object(ExclusiveVMLock, "_host_memory", return_value=(64 * 1024**3, 63 * 1024**3)),
            patch.object(ExclusiveVMLock, "_process_rss", return_value=0),
            patch.object(ExclusiveVMLock, "_registered_vm_resources", return_value={}),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_same_writable_disk_cannot_be_reserved_by_two_vms(self):
        disk = Path(self.temp.name) / "shared.qcow2"
        first = ExclusiveVMLock()
        second = ExclusiveVMLock()

        self.assertTrue(first.acquire("First", 4, "first", disk_path=disk))
        self.assertFalse(second.acquire("Second", 4, "second", disk_path=disk))
        self.assertEqual(second.last_error["reason"], "resource_conflict")
        self.assertEqual(second.last_error["resources"], ["disk"])
        self.assertEqual(second.last_error["active_vms"], ["First"])
        first.release()

    def test_same_vm_cannot_be_admitted_twice(self):
        first = ExclusiveVMLock()
        second = ExclusiveVMLock()
        self.assertTrue(first.acquire("First", 4, "same-vm"))
        self.assertFalse(second.acquire("First", 4, "same-vm"))
        self.assertEqual(second.last_error["reason"], "already_running")
        first.release()

    def test_memory_pressure_rejects_a_second_vm_without_leaking_reservation(self):
        first = ExclusiveVMLock()
        second = ExclusiveVMLock()
        self.assertTrue(first.acquire("First", 4, "first"))
        with patch.object(ExclusiveVMLock, "_host_memory", return_value=(16 * 1024**3, 1 * 1024**3)):
            self.assertFalse(second.acquire("Second", 4, "second"))
        self.assertIn("requested_gb", second.last_error)
        self.assertIn("active_vms", second.last_error)
        self.assertEqual(len(ExclusiveVMLock._read_reservations()), 1)
        first.release()

    def test_reservation_disk_full_has_a_stable_admission_reason(self):
        lock = ExclusiveVMLock()
        with patch.object(
            ExclusiveVMLock,
            "_write_reservation",
            side_effect=OSError(errno.ENOSPC, "No space left on device"),
        ):
            self.assertFalse(lock.acquire("Studio", 4, "studio"))
        self.assertEqual(lock.last_error, {
            "reason": "storage_full",
            "resource": "vm_reservation",
        })

    def test_same_usb_device_cannot_be_reserved_by_two_vms(self):
        first = ExclusiveVMLock()
        second = ExclusiveVMLock()

        self.assertTrue(first.acquire("First", 4, "first", usb_devices=["1050:0407"]))
        self.assertFalse(second.acquire("Second", 4, "second", usb_devices=["1050:0407"]))
        self.assertEqual(second.last_error["resources"], ["usb"])
        first.release()

    def test_same_forwarded_ssh_port_cannot_be_reserved_by_two_vms(self):
        first = ExclusiveVMLock()
        second = ExclusiveVMLock()

        self.assertTrue(first.acquire("First", 4, "first", network_ports=[2222]))
        self.assertFalse(second.acquire("Second", 4, "second", network_ports=[2222]))
        self.assertEqual(second.last_error["reason"], "resource_conflict")
        self.assertEqual(second.last_error["resources"], ["network_port"])
        first.release()

    def test_distinct_forwarded_ssh_ports_remain_concurrent(self):
        first = ExclusiveVMLock()
        second = ExclusiveVMLock()

        self.assertTrue(first.acquire("First", 4, "first", network_ports=[2222]))
        self.assertTrue(second.acquire("Second", 4, "second", network_ports=[2223]))
        first.release()
        second.release()

    def test_host_port_already_in_use_rejects_ssh_forward_explicitly(self):
        lock = ExclusiveVMLock()
        with patch.object(ExclusiveVMLock, "_loopback_tcp_port_available", return_value=False):
            self.assertFalse(lock.acquire("First", 4, "first", network_ports=[2222]))
        self.assertEqual(lock.last_error["reason"], "network_port_in_use")
        self.assertEqual(lock.last_error["network_ports"], [2222])

    def test_shared_additional_writable_disk_cannot_be_reserved_by_two_vms(self):
        shared = Path(self.temp.name) / "shared-additional.qcow2"
        first = ExclusiveVMLock()
        second = ExclusiveVMLock()

        self.assertTrue(first.acquire("First", 4, "first", disk_paths=[shared]))
        self.assertFalse(second.acquire("Second", 4, "second", disk_paths=[shared]))
        self.assertEqual(second.last_error["reason"], "resource_conflict")
        self.assertEqual(second.last_error["resources"], ["disk"])
        first.release()

    def test_primary_and_additional_disk_claims_share_one_reservation(self):
        primary = Path(self.temp.name) / "primary.qcow2"
        additional = Path(self.temp.name) / "additional.qcow2"
        lock = ExclusiveVMLock()
        self.assertTrue(lock.acquire("First", 4, "first", disk_path=primary, disk_paths=[additional]))
        row = ExclusiveVMLock._read_reservations()[0]
        self.assertEqual(set(row["disk_keys"]), {
            ExclusiveVMLock._disk_resource_key(primary),
            ExclusiveVMLock._disk_resource_key(additional),
        })
        lock.release()

    def test_distinct_vfio_devices_in_the_same_iommu_group_conflict(self):
        sysfs = Path(self.temp.name) / "sysfs"
        devices_root = sysfs / "bus/pci/devices"
        groups_root = sysfs / "kernel/iommu_groups"
        group = groups_root / "17"
        group.mkdir(parents=True)
        bdfs = ("0000:01:00.0", "0000:01:00.1")
        for bdf in bdfs:
            device = devices_root / bdf
            device.mkdir(parents=True)
            (device / "iommu_group").symlink_to(group)
        with patch("core.config.SYSFS_PCI_DEVICES", devices_root), \
             patch("core.config.SYSFS_IOMMU_GROUPS", groups_root):
            first = ExclusiveVMLock()
            second = ExclusiveVMLock()
            self.assertTrue(first.acquire("First", 4, "first", vfio_devices=[bdfs[0]]))
            self.assertFalse(second.acquire("Second", 4, "second", vfio_devices=[bdfs[1]]))
            self.assertEqual(second.last_error["reason"], "resource_conflict")
            self.assertEqual(second.last_error["resources"], ["vfio"])
            first.release()

    def test_vfio_admission_fails_closed_when_iommu_group_is_unavailable(self):
        devices_root = Path(self.temp.name) / "missing-devices"
        groups_root = Path(self.temp.name) / "missing-groups"
        devices_root.mkdir()
        groups_root.mkdir()
        with patch("core.config.SYSFS_PCI_DEVICES", devices_root), \
             patch("core.config.SYSFS_IOMMU_GROUPS", groups_root):
            lock = ExclusiveVMLock()
            self.assertFalse(lock.acquire("VFIO", 4, "vfio", vfio_devices=["0000:01:00.0"]))
            self.assertIn("IOMMU", lock.last_error["reason"])

    def test_distinct_disks_and_usb_devices_remain_concurrent(self):
        first = ExclusiveVMLock()
        second = ExclusiveVMLock()

        self.assertTrue(first.acquire(
            "First", 4, "first", disk_path=Path(self.temp.name) / "a.qcow2",
            usb_devices=["1050:0407"],
        ))
        self.assertTrue(second.acquire(
            "Second", 4, "second", disk_path=Path(self.temp.name) / "b.qcow2",
            usb_devices=["046d:c534"],
        ))
        first.release()
        second.release()

    def test_concurrent_admission_serializes_a_shared_writable_disk(self):
        barrier = threading.Barrier(2)
        disk = Path(self.temp.name) / "shared-race.qcow2"

        def reserve(vm_id: str):
            lock = ExclusiveVMLock()
            barrier.wait(timeout=2)
            admitted = lock.acquire(vm_id, 4, vm_id, disk_path=disk)
            return lock, admitted

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(reserve, ("race-a", "race-b")))

        admitted_locks = [lock for lock, admitted in results if admitted]
        self.assertEqual(len(admitted_locks), 1)
        for lock in admitted_locks:
            lock.release()

    def test_concurrent_admission_starts_the_same_vm_only_once(self):
        barrier = threading.Barrier(2)

        def reserve():
            lock = ExclusiveVMLock()
            barrier.wait(timeout=2)
            admitted = lock.acquire("Studio", 4, "studio")
            return lock, admitted

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _index: reserve(), range(2)))

        admitted_locks = [lock for lock, admitted in results if admitted]
        rejected_locks = [lock for lock, admitted in results if not admitted]
        self.assertEqual(len(admitted_locks), 1)
        self.assertEqual(len(rejected_locks), 1)
        self.assertEqual(rejected_locks[0].last_error["reason"], "already_running")
        self.assertEqual(len(ExclusiveVMLock._read_reservations()), 1)
        for lock in admitted_locks:
            lock.release()

    def test_invalid_usb_claim_is_ignored_and_never_becomes_a_lock_key(self):
        lock = ExclusiveVMLock()
        self.assertTrue(lock.acquire("First", 4, "first", usb_devices=["not-a-device"]))
        row = ExclusiveVMLock._read_reservations()[0]
        self.assertEqual(row["usb_devices"], [])
        lock.release()

    def test_vm_config_validates_additional_disk_contract_and_keeps_legacy_default(self):
        legacy = VMConfig.from_dict({"id": "legacy", "name": "Legacy", "macos_version": "tahoe"})
        self.assertIsNone(legacy.additional_disks)
        vm = VMConfig.from_dict({
            "id": "with-data", "name": "With data", "macos_version": "tahoe",
            "additional_disks": [{
                "path": "/srv/media/data.qcow2", "format": "qcow2", "controller": "nvme", "order": 0,
            }],
        })
        self.assertEqual(vm.additional_disks[0].path, "/srv/media/data.qcow2")
        for invalid in (
            [{"path": "../data.qcow2", "controller": "nvme", "order": 0}],
            [{"path": "/data.qcow2", "format": "vhdx", "controller": "nvme", "order": 0}],
            [{"path": "/data.qcow2", "controller": "scsi", "order": 0}],
            [{"path": "/data.qcow2", "controller": "nvme", "order": 1}],
            [
                {"path": "/data.qcow2", "controller": "nvme", "order": 0},
                {"path": "/data.qcow2", "controller": "ahci", "order": 1},
            ],
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                VMConfig.from_dict({
                    "id": "invalid", "name": "Invalid", "macos_version": "tahoe",
                    "additional_disks": invalid,
                })

    def test_running_vm_without_reservation_claims_registered_disk_and_usb(self):
        catalog = Path(self.temp.name) / "catalog.json"
        catalog.write_text(json.dumps({"vms": {
            "legacy": {
                "id": "legacy", "name": "Legacy", "macos_version": "tahoe",
                "ram_gb": 4, "disk_path": str(Path(self.temp.name) / "shared.qcow2"),
                "additional_disks": [{
                    "path": str(Path(self.temp.name) / "shared-extra.qcow2"),
                    "format": "qcow2", "controller": "nvme", "order": 0,
                }],
                "usb_auto_attach": ["1050:0407"],
            },
        }}))
        catalog.chmod(0o640)
        reims_vm_dir = Path(self.temp.name) / "legacy-reims-vm"
        reims_vm_dir.mkdir()
        (reims_vm_dir / "vm.toml").write_text(
            'disk = "system.qcow2"\n[[additional_disks]]\npath = "shared-extra.qcow2"\n'
        )
        self.patches.pop().stop()  # Exercise the real catalog resource reader.
        with patch.dict(os.environ, {"VHACKINTOSH_VM_CATALOG": str(catalog)}), \
             patch.object(ExclusiveVMLock, "_running_reims_vms", return_value=({"legacy": 123}, True)), \
             patch("core.reims_ctl.vm_record", side_effect=lambda vm_id: {"dir": str(reims_vm_dir)} if vm_id == "legacy" else None):
            claims = ExclusiveVMLock._registered_vm_resources("legacy")
            self.assertIn(str(reims_vm_dir / "shared-extra.qcow2"), claims.get("disk_paths", []), claims)
            second = ExclusiveVMLock()
            self.assertFalse(second.acquire(
                "Second", 4, "second", disk_path=Path(self.temp.name) / "shared.qcow2",
                usb_devices=["046d:c534"],
            ))
            self.assertEqual(second.last_error["resources"], ["disk"])
            self.assertEqual(second.last_error["active_vms"], ["Legacy"])

            extra_disk = ExclusiveVMLock()
            self.assertFalse(extra_disk.acquire(
                "Fourth", 4, "fourth", disk_path=reims_vm_dir / "shared-extra.qcow2",
            ))
            self.assertEqual(extra_disk.last_error.get("resources"), ["disk"], extra_disk.last_error)

            by_usb = ExclusiveVMLock()
            self.assertFalse(by_usb.acquire(
                "Third", 4, "third", disk_path=Path(self.temp.name) / "other.qcow2",
                usb_devices=["1050:0407"],
            ))
            self.assertEqual(by_usb.last_error["resources"], ["usb"])
            self.assertEqual(catalog.stat().st_mode & 0o777, 0o640)


if __name__ == "__main__":
    unittest.main()
