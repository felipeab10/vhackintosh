"""Headless VM provisioning tests with all host-facing dependencies replaced."""

from __future__ import annotations

import threading
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.config import CpuTopologyConfig, SMBIOSConfig
from core.vm_provisioning import (
    InstallerPolicy,
    ProvisioningCancelled,
    ProvisioningEvent,
    ProvisioningTimedOut,
    VMProvisioningRequest,
    VMProvisioningService,
)


class FakeStore:
    def __init__(self):
        self.vms = []
        self.revision = "r1"

    def list_vms(self):
        return list(self.vms)

    def add_vm(self, vm, **kwargs):
        self.vms.append(vm)


class FakeDiskProvisioner:
    def __init__(self, root, after_provision=None):
        self.root = Path(root)
        self.after_provision = after_provision
        self.calls = []

    def provision(self, vm, *, installer_img=None, job_context=None):
        self.calls.append((vm, installer_img))
        self.last_job_context = job_context
        vm_dir = self.root / vm.id
        vm_dir.mkdir(parents=True, exist_ok=True)
        disk = vm_dir / "macos.qcow2"
        efi = vm_dir / "OpenCore.qcow2"
        disk.touch()
        efi.touch()
        if self.after_provision:
            self.after_provision()
        return SimpleNamespace(
            vm_dir=vm_dir,
            hdd_path=disk,
            opencore_path=efi,
            installer_path=Path(installer_img) if installer_img else None,
        )


class FakeSMBIOS:
    def generate(self, version):
        return SMBIOSConfig(
            model="iMac20,1",
            serial_number="C02TESTSERIAL",
            board_serial="C0270000000000001",
            smuuid="00000000-0000-4000-8000-000000000001",
            rom="02AABBCCDDEE",
        )


class SimpleJobContext:
    def __init__(self, *, validation_completed=False):
        self.validation_completed = validation_completed

    def completed(self, name):
        return name == "validation" and self.validation_completed

    def check_cancelled(self):
        return None


def make_request(**changes):
    values = dict(
        name="macOS Lab",
        macos_version="sequoia",
        vcpus=8,
        ram_gb=12,
        disk_size_gb=80,
        installer_policy=InstallerPolicy.NONE,
    )
    values.update(changes)
    return VMProvisioningRequest(**values)


class VMProvisioningServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = FakeStore()
        self.disk = FakeDiskProvisioner(self.root)
        self.downloads = []
        self.service = VMProvisioningService(
            store=self.store,
            disk_provisioner=self.disk,
            smbios_generator=FakeSMBIOS(),
            cached_installer=lambda version: None,
            prepare_installer=lambda version: self.downloads.append(version),
            gpu_validator=lambda slot: (True, ""),
            storage_root=self.root,
            id_factory=lambda: "a1b2c3d4",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_provisions_and_registers_only_after_disk_and_opencore_exist(self):
        events = []
        result = self.service.provision(make_request(auto_start=True), on_event=events.append)

        self.assertEqual(result.vm.id, "macos-lab-a1b2c3d4")
        self.assertEqual(result.vm.disk_path, str(result.bundle.hdd_path))
        self.assertEqual(result.vm.power_sync_mode, "vm-only")
        self.assertTrue(result.vm.auto_start)
        self.assertEqual(self.store.vms, [result.vm])
        self.assertEqual([event.progress for event in events], [10, 30, 45, 50, 90, 100])
        self.assertTrue(result.bundle.hdd_path.is_file())

    def test_automatic_identifier_retries_if_generated_candidate_is_already_reserved(self):
        (self.root / "macos-lab-a1b2c3d4").mkdir()
        generated = iter(("a1b2c3d4", "d4e5f607"))
        self.service.id_factory = lambda: next(generated)

        vm_id = self.service._reserve_id(None, "macOS Lab")

        self.assertEqual(vm_id, "macos-lab-d4e5f607")

    def test_persists_display_preferences_from_provisioning_request(self):
        result = self.service.provision(make_request(
            display_resolution="2560x1440", display_hidpi=True, display_refresh_hz=144,
        ))
        self.assertEqual(result.vm.display_resolution, "2560x1440")
        self.assertTrue(result.vm.display_hidpi)
        self.assertEqual(result.vm.display_refresh_hz, 144)

    def test_persists_qemu_cpu_model(self):
        result = self.service.provision(make_request(cpu_model="Haswell-noTSX"))
        self.assertEqual(result.vm.cpu_model, "Haswell-noTSX")

    def test_persists_optional_cpu_topology(self):
        result = self.service.provision(make_request(
            vcpus=4, cpu_topology=CpuTopologyConfig(sockets=2, cores=2, threads=1),
        ))
        self.assertEqual(result.vm.cpu_topology, CpuTopologyConfig(sockets=2, cores=2, threads=1))

    def test_persists_usb_auto_attach_preferences(self):
        result = self.service.provision(make_request(usb_auto_attach=["1234:abcd"]))
        self.assertEqual(result.vm.usb_auto_attach, ["1234:abcd"])

    def test_resolves_cached_installer_without_downloading(self):
        installer = self.root / "BaseSystem.img"
        installer.touch()
        self.service.cached_installer = lambda version: installer
        result = self.service.provision(make_request(installer_policy=InstallerPolicy.CACHED))

        self.assertEqual(result.installer_path, installer)
        self.assertTrue(result.vm.opencore_show_picker)
        self.assertEqual(self.downloads, [])

    def test_download_occurs_only_when_policy_explicitly_requests_it(self):
        installer = self.root / "downloaded.img"
        installer.touch()
        self.service.prepare_installer = lambda version: (self.downloads.append(version), installer)[1]
        result = self.service.provision(make_request(installer_policy="download"))

        self.assertEqual(result.installer_path, installer)
        self.assertEqual(self.downloads, ["sequoia"])

    def test_web_job_context_reaches_disk_provisioner(self):
        context = SimpleJobContext()
        self.service.provision(make_request(), job_context=context)
        self.assertIs(self.disk.last_job_context, context)

    def test_interrupted_provision_retry_reuses_only_same_job_directory(self):
        from core.web_job_runner import JobCancelledError

        class CancelOnceDisk(FakeDiskProvisioner):
            def __init__(self, root):
                super().__init__(root)
                self.attempts = 0

            def provision(self, vm, *, installer_img=None, job_context=None):
                self.attempts += 1
                if self.attempts == 1:
                    raise JobCancelledError("cancelled while creating disk")
                return super().provision(vm, installer_img=installer_img, job_context=job_context)

        disk = CancelOnceDisk(self.root)
        self.service.disk_provisioner = disk
        request = make_request(vm_id="retry-vm")
        with self.assertRaisesRegex(JobCancelledError, "cancelled while creating disk"):
            self.service.provision(request, job_context=SimpleJobContext())
        self.assertTrue((self.root / "retry-vm").is_dir())
        self.assertEqual(self.store.vms, [])

        result = self.service.provision(
            request, job_context=SimpleJobContext(validation_completed=True)
        )
        self.assertEqual(result.vm.id, "retry-vm")
        self.assertEqual(self.store.vms, [result.vm])

    def test_job_cancellation_from_disk_is_not_wrapped_as_provisioning_failure(self):
        from core.web_job_runner import JobCancelledError

        class CancelledDisk(FakeDiskProvisioner):
            def provision(self, vm, *, installer_img=None, job_context=None):
                raise JobCancelledError("cancelled while running qemu-img")

        self.service.disk_provisioner = CancelledDisk(self.root)
        with self.assertRaisesRegex(JobCancelledError, "cancelled while running qemu-img"):
            self.service.provision(make_request(), job_context=SimpleJobContext())
        self.assertEqual(self.store.vms, [])

    def test_missing_cached_media_fails_without_download(self):
        with self.assertRaisesRegex(RuntimeError, "cache"):
            self.service.provision(make_request(installer_policy=InstallerPolicy.CACHED))
        self.assertEqual(self.downloads, [])
        self.assertEqual(self.disk.calls, [])

    def test_rejects_gpu_selection_that_cannot_be_uniquely_applied(self):
        self.service.gpu_validator = lambda slot: (False, "ambiguous ICD")
        with self.assertRaisesRegex(ValueError, "ambiguous ICD"):
            self.service.provision(make_request(selected_gpu="0000:01:00.0"))
        self.assertEqual(self.disk.calls, [])

    def test_rejects_an_existing_requested_id_without_touching_its_files(self):
        existing = self.root / "existing-vm"
        existing.mkdir()
        marker = existing / "user-data"
        marker.write_text("preserve", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "existing VM artifacts"):
            self.service.provision(make_request(vm_id="existing-vm"))

        self.assertEqual(marker.read_text(encoding="utf-8"), "preserve")
        self.assertEqual(self.disk.calls, [])

    def test_cancellation_after_disk_keeps_artifacts_and_does_not_register(self):
        cancel = threading.Event()
        self.disk.after_provision = cancel.set

        with self.assertRaises(ProvisioningCancelled):
            self.service.provision(make_request(vm_id="cancelled-vm"), cancel_event=cancel)

        self.assertTrue((self.root / "cancelled-vm" / "macos.qcow2").exists())
        self.assertEqual(self.store.vms, [])

    def test_timeout_after_disk_keeps_artifacts_and_does_not_register(self):
        ticks = iter([0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 2.0])
        self.service.clock = lambda: next(ticks)

        with self.assertRaises(ProvisioningTimedOut):
            self.service.provision(make_request(vm_id="timed-out-vm"), timeout_sec=1)

        self.assertTrue((self.root / "timed-out-vm" / "macos.qcow2").exists())
        self.assertEqual(self.store.vms, [])

    def test_checks_cancellation_before_any_side_effect(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(ProvisioningCancelled):
            self.service.provision(make_request(), cancel_event=cancel)
        self.assertEqual(self.disk.calls, [])
        self.assertEqual(self.store.vms, [])

    def test_reports_creation_failure_without_registering(self):
        self.disk.provision = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("qemu-img unavailable"))

        with self.assertRaisesRegex(RuntimeError, "qemu-img unavailable"):
            self.service.provision(make_request())

        self.assertEqual(self.store.vms, [])

    def test_validates_resource_limits_before_host_operations(self):
        with self.assertRaisesRegex(ValueError, "vcpus"):
            self.service.provision(make_request(vcpus=0))
        with self.assertRaisesRegex(ValueError, "disk_size_gb"):
            self.service.provision(make_request(disk_size_gb=8))
        self.assertEqual(self.disk.calls, [])

    def test_callback_receives_structured_events(self):
        observed = []
        self.service.provision(make_request(), on_event=observed.append)

        self.assertTrue(all(isinstance(event, ProvisioningEvent) for event in observed))
        self.assertEqual(observed[-1].stage, "catalog")
        self.assertEqual(observed[-1].status, "completed")


if __name__ == "__main__":
    unittest.main()
