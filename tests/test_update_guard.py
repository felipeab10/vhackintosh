import tempfile
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from core.config import ExclusiveVMLock
from core import reims_build
from core.update_guard import MaintenanceBlocked, maintenance_window


class MaintenanceGuardTests(unittest.TestCase):
    def test_active_vm_blocks_maintenance_and_does_not_leave_marker(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            marker = root / "run/maintenance.json"
            with patch.object(ExclusiveVMLock, "_running_reims_vms", return_value=({"vm-a": 123}, True)):
                with self.assertRaisesRegex(MaintenanceBlocked, "vm-a"):
                    with maintenance_window(marker=marker, lock_path=root / "lock",
                                            reservations_dir=root / "reservations"):
                        self.fail("maintenance must be refused")
            self.assertFalse(marker.exists())

    def test_unknown_vm_state_blocks_maintenance(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.object(ExclusiveVMLock, "_running_reims_vms", return_value=({}, False)), \
                 patch("core.update_guard._runtime_process_present", return_value=True):
                with self.assertRaisesRegex(MaintenanceBlocked, "não foi possível confirmar"):
                    with maintenance_window(marker=root / "run/maintenance.json",
                                            lock_path=root / "lock",
                                            reservations_dir=root / "reservations"):
                        self.fail("unknown VM state must fail closed")

    def test_missing_controller_allows_clean_first_install_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.object(ExclusiveVMLock, "_running_reims_vms", return_value=({}, False)), \
                 patch("core.update_guard._runtime_process_present", return_value=False):
                with maintenance_window(marker=root / "run/maintenance.json",
                                        lock_path=root / "lock",
                                        reservations_dir=root / "reservations"):
                        self.assertTrue((root / "run/maintenance.json").exists())

    def test_runtime_install_can_ignore_only_its_own_vm_reservation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            marker = root / "run/maintenance.json"
            reservations = root / "reservations"
            reservations.mkdir()
            (reservations / "own.json").write_text(json.dumps({
                "owner_pid": os.getpid(), "vm_id": "vm-own", "name": "Minha VM",
            }))
            with patch.object(ExclusiveVMLock, "_running_reims_vms", return_value=({}, True)):
                with maintenance_window(
                    marker=marker,
                    lock_path=root / "lock",
                    reservations_dir=reservations,
                    reservation_vm_id="vm-own",
                ):
                    self.assertTrue(marker.exists())
                self.assertFalse(marker.exists())

    def test_runtime_install_still_blocks_other_vm_reservations(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            reservations = root / "reservations"
            reservations.mkdir()
            (reservations / "other.json").write_text(json.dumps({
                "owner_pid": os.getpid(), "vm_id": "vm-other", "name": "Outra VM",
            }))
            with patch.object(ExclusiveVMLock, "_running_reims_vms", return_value=({}, True)):
                with self.assertRaisesRegex(MaintenanceBlocked, "Outra VM"):
                    with maintenance_window(
                        marker=root / "run/maintenance.json",
                        lock_path=root / "lock",
                        reservations_dir=reservations,
                        reservation_vm_id="vm-own",
                    ):
                        self.fail("other VM reservations must continue blocking runtime maintenance")

    def test_missing_controller_with_qemu_process_blocks_maintenance(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.object(ExclusiveVMLock, "_running_reims_vms", return_value=({}, False)), \
                 patch("core.update_guard._runtime_process_present", return_value=True):
                with self.assertRaisesRegex(MaintenanceBlocked, "estado das VMs"):
                    with maintenance_window(marker=root / "run/maintenance.json",
                                            lock_path=root / "lock",
                                            reservations_dir=root / "reservations"):
                        self.fail("QEMU process must block maintenance")

    def test_marker_blocks_vm_admission_for_full_maintenance_window(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            marker = root / "run/maintenance.json"
            with patch("core.config.VM_MAINTENANCE_MARKER", marker), \
                 patch.object(ExclusiveVMLock, "_running_reims_vms", return_value=({}, True)):
                with maintenance_window(marker=marker, lock_path=root / "lock",
                                        reservations_dir=root / "reservations"):
                    admission = ExclusiveVMLock()
                    self.assertFalse(admission.acquire("VM", 1, "vm-a"))
                    self.assertEqual(admission.last_error["reason"], "maintenance")
                self.assertFalse(marker.exists())

    def test_failed_bundle_promotion_keeps_current_runtime(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = root / "release-1"
            second = root / "release-2"
            for tree, tag in ((first, "v1.0.1"), (second, "v1.0.2")):
                qemu = tree / "vendor/qemu/build/bin/qemu-system-x86_64"
                qemu.parent.mkdir(parents=True)
                qemu.write_bytes(b"#!/bin/sh\n# QEMU emulator version 11.1.50\napple-gfx-pci ReimsVgpu.rom\n")
                qemu.chmod(0o755)
                rom = tree / "vendor/qemu/build/share/qemu/ReimsVgpu.rom"
                rom.parent.mkdir(parents=True)
                rom.write_bytes(b"rom")
                (tree / ".release").write_text(tag)
            data = root / "runtime"
            with patch.object(reims_build, "qemu_data_dir", return_value=data), \
                 patch.object(reims_build, "reims_ctl_install_dir", return_value=root / "bin"):
                self.assertTrue(reims_build.install_prebuilt(first))
                current = data / "qemu/current"
                self.assertEqual(current.resolve().name, "v1.0.1")
                original_replace = reims_build.os.replace

                def fail_new_version(source, destination):
                    if Path(destination) == data / "qemu/v1.0.2":
                        raise OSError("simulated rename failure")
                    return original_replace(source, destination)

                with patch.object(reims_build.os, "replace", side_effect=fail_new_version):
                    self.assertFalse(reims_build.install_prebuilt(second))
                self.assertEqual(current.resolve().name, "v1.0.1")
                self.assertTrue((data / "qemu/v1.0.1/bin/qemu-system-x86_64").is_file())
                self.assertFalse((data / "qemu/v1.0.2").exists())


if __name__ == "__main__":
    unittest.main()
