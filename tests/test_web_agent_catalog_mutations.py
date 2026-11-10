from __future__ import annotations

import tempfile
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from core.config import SMBIOSConfig, VMConfig, VMManagerStore
from core.graphics_profiles import EMBEDDED_KEXTS, GRAPHICS_PROFILES
from core.web_agent import WebAgentService
from core.web_rpc import RpcRemoteError

LEGACY = "apple-gfx-pci"
REIMS = "reims-graphics-pci"

def _kexts(profile):
    """Exact enabled-kext set the closed contract declares for a profile."""
    return set(EMBEDDED_KEXTS) | {bundle for bundle, _minimum in GRAPHICS_PROFILES[profile]}

def _runtime(profile=LEGACY, kext_mode="embedded"):
    return {
        "reims_update_opencore_picker": True,
        "reims_update_opencore_kext_mode": kext_mode,
        "reims_update_opencore_kext_mode_source": "/managed/reims-util",
        "qemu_graphics_profile": profile,
        "qemu_graphics_profile_source": "/managed/qemu-system-x86_64",
    }


class WebAgentCatalogMutationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.catalog = Path(self.temp.name) / "vms.json"
        self.service = WebAgentService(
            self.catalog,
            running_vms=lambda: {},
            smbios_factory=lambda _version: SMBIOSConfig(
                model="iMac20,1", serial_number="C02TEST12345", board_serial="C02TEST1234567890",
                smuuid="11111111-2222-3333-4444-555555555555", rom="020000000001",
            ),
        )
        self.capacity = patch("core.web_agent.psutil.cpu_count", return_value=8)
        self.memory = patch("core.web_agent.psutil.virtual_memory")
        self.capacity.start()
        mem = self.memory.start()
        mem.return_value.total = 16 * 1024**3

    def tearDown(self):
        self.capacity.stop()
        self.memory.stop()
        self.temp.cleanup()

    def draft(self, **overrides):
        return {
            "id": "macos-studio",
            "name": "Studio",
            "macos_version": "tahoe",
            "vcpus": 6,
            "cpu_model": "Skylake-Client-noTSX-IBRS",
            "ram_gb": 8,
            "disk_size_gb": 96,
            "auto_start": False,
            "audio_device": "usb-audio",
            "fullscreen": True,
            "opencore_show_picker": False,
            "gpu_mode": "reims-vgpu",
            "selected_gpu": None,
            "selected_gpu_uuid": None,
            "network_enabled": True,
            "power_sync_mode": "vm-only",
            "qemu_extra_args": [],
            **overrides,
        }

    def test_create_requires_current_revision_and_creates_server_owned_identity(self):
        initial_revision = self.service.store.revision
        result = self.service.handlers()["vms.config.create"]({
            "config": self.draft(), "expected_revision": f'"{initial_revision}"',
        })
        self.assertEqual(result["config"]["id"], "macos-studio")
        self.assertEqual(result["config"]["smbios"]["serial_number"], "C02TEST12345")
        self.assertEqual(result["config"]["power_sync_mode"], "vm-only")
        self.assertNotEqual(result["revision"], initial_revision)

    def test_update_preserves_unknown_legacy_fields(self):
        store = VMManagerStore(self.catalog)
        store.add_vm(VMConfig.from_dict({
            **self.draft(),
            "id": "macos-studio",
            "smbios": {"model": "iMac20,1", "serial_number": "S1", "board_serial": "B1", "smuuid": "U1", "rom": "R1"},
            "extra_legacy_option": {"preserve": True},
        }))
        revision = store.revision
        update = {key: value for key, value in self.draft(name="Renamed").items() if key != "id"}
        result = self.service.handlers()["vms.config.update"]({
            "vm_id": "macos-studio", "config": update, "expected_revision": f'"{revision}"',
        })
        self.assertEqual(result["config"]["name"], "Renamed")
        self.assertTrue(result["config"]["extra_config"]["extra_legacy_option"]["preserve"])

    def test_additional_disk_resolver_accepts_only_managed_media_when_reims_schema_is_available(self):
        from types import SimpleNamespace

        from core.config import AdditionalDiskConfig

        vm = VMConfig(id="macos-studio", name="Studio", macos_version="tahoe")
        self.service.store.add_vm(vm)
        media_root = self.service.store.config_dir / "media"
        media_root.mkdir(parents=True)
        image = media_root / "data.raw"
        image.write_bytes(b"managed image")
        item = next(item for item in self.service.storage_inventory({})["items"] if item["name"] == image.name)

        with patch("core.runtime_inventory.inspect_runtime", return_value={"reims_schema_features": ["additional_disks"]}):
            resolved = self.service._resolve_additional_disks(
                [SimpleNamespace(media_id=item["id"], controller="nvme", order=0)], existing=vm,
            )

        self.assertEqual(resolved, [AdditionalDiskConfig(path=str(image.resolve()), format="raw", controller="nvme", order=0)])

    def test_additional_disk_resolver_fails_closed_without_reims_schema_feature(self):
        from types import SimpleNamespace

        vm = VMConfig(id="macos-studio", name="Studio", macos_version="tahoe")
        with patch("core.runtime_inventory.inspect_runtime", return_value={"reims_schema_features": []}):
            with self.assertRaises(RpcRemoteError) as raised:
                self.service._resolve_additional_disks(
                    [SimpleNamespace(media_id="a" * 32, controller="nvme", order=0)], existing=vm,
                )
        self.assertEqual(raised.exception.code, "additional_disks_unavailable")

    def test_config_envelope_reports_fields_pending_since_last_reims_projection(self):
        store = VMManagerStore(self.catalog)
        vm = VMConfig(
            id="macos-studio", name="Studio", macos_version="tahoe",
            vcpus=6, ram_gb=8, fullscreen=False, display_resolution="1920x1080",
        )
        store.add_vm(vm)
        vm_dir = store.config_dir / "vms" / vm.id
        vm_dir.mkdir(parents=True)
        (vm_dir / "vm.toml").write_text(
            'cpus = 4\nmemory_mib = 4096\n[display]\nresolution = "1280x720"\nhidpi = false\nborderless = true\n[q35]\ncpu_model = "Haswell-noTSX"\n',
            encoding="utf-8",
        )

        envelope = self.service.get_vm_config({"vm_id": vm.id})

        self.assertTrue(envelope["runtime_config_differs"])
        self.assertEqual(envelope["pending_restart_fields"], ["vcpus", "cpu_model", "ram_gb", "display_resolution", "fullscreen"])
        self.assertEqual(envelope["projection_status"], "available")
        self.assertEqual(
            [(item["field"], item["saved_value"], item["last_projection_value"]) for item in envelope["config_differences"]],
            [("vcpus", "6", "4"), ("cpu_model", "Skylake-Client-noTSX-IBRS", "Haswell-noTSX"),
             ("ram_gb", "8", "4.0"), ("display_resolution", "1920x1080", "1280x720"),
             ("fullscreen", "False", "True")],
        )

    def test_config_envelope_reports_advanced_qemu_tokens_pending_until_next_projection(self):
        store = VMManagerStore(self.catalog)
        vm = VMConfig(id="macos-studio", name="Studio", macos_version="tahoe")
        vm.qemu_extra_args = ["-rtc", "base=utc"]
        store.add_vm(vm)
        vm_dir = store.config_dir / "vms" / vm.id
        vm_dir.mkdir(parents=True)
        (vm_dir / "vm.toml").write_text(
            'extra_args = ["-action", "reboot=shutdown"]\n', encoding="utf-8",
        )

        envelope = self.service.get_vm_config({"vm_id": vm.id})

        self.assertEqual(envelope["pending_restart_fields"], ["qemu_extra_args"])
        self.assertEqual(envelope["config_differences"][0]["saved_value"], '["-rtc","base=utc"]')
        self.assertEqual(envelope["config_differences"][0]["last_projection_value"], "[]")

    def test_config_envelope_ignores_unsafe_or_invalid_projection(self):
        store = VMManagerStore(self.catalog)
        vm = VMConfig(id="macos-studio", name="Studio", macos_version="tahoe")
        store.add_vm(vm)
        vm_dir = store.config_dir / "vms" / vm.id
        vm_dir.mkdir(parents=True)
        (vm_dir / "vm.toml").write_text("not = [valid toml", encoding="utf-8")

        envelope = self.service.get_vm_config({"vm_id": vm.id})

        self.assertFalse(envelope["runtime_config_differs"])
        self.assertEqual(envelope["pending_restart_fields"], [])
        self.assertEqual(envelope["projection_status"], "unknown")

        outside = self.catalog.parent / "outside-vm.toml"
        outside.write_text('cpus = 1\n[display]\nborderless = false\n', encoding="utf-8")
        (vm_dir / "vm.toml").unlink()
        (vm_dir / "vm.toml").symlink_to(outside)
        envelope = self.service.get_vm_config({"vm_id": vm.id})
        self.assertFalse(envelope["runtime_config_differs"])
        self.assertEqual(envelope["pending_restart_fields"], [])

    def test_update_persists_supported_autostart_and_display_options(self):
        store = VMManagerStore(self.catalog)
        vm = VMConfig(id="macos-studio", name="Studio", macos_version="tahoe", smbios=SMBIOSConfig())
        store.add_vm(vm)
        update = {
            key: value for key, value in self.draft(auto_start=True, fullscreen=False, name="Studio janela").items()
            if key != "id"
        }

        with patch("core.runtime_inventory.inspect_runtime", return_value={"reims_set_options": ["--audio"]}):
            result = self.service.handlers()["vms.config.update"]({
                "vm_id": vm.id, "config": update, "expected_revision": f'"{store.revision}"',
            })

        self.assertTrue(result["config"]["auto_start"])
        self.assertFalse(result["config"]["fullscreen"])
        self.assertEqual(self.service.store.get_vm(vm.id).name, "Studio janela")

    def test_cpu_model_change_requires_detected_qemu_model_and_is_persisted(self):
        store = VMManagerStore(self.catalog)
        vm = VMConfig(id="macos-studio", name="Studio", macos_version="tahoe", smbios=SMBIOSConfig())
        store.add_vm(vm)
        update = {key: value for key, value in self.draft(cpu_model="Haswell-noTSX").items() if key != "id"}
        with patch("core.runtime_inventory.inspect_runtime", return_value={"qemu_cpu_models": ["Haswell-noTSX"]}):
            result = self.service.handlers()["vms.config.update"]({
                "vm_id": vm.id, "config": update, "expected_revision": f'"{store.revision}"',
            })
        self.assertEqual(result["config"]["cpu_model"], "Haswell-noTSX")
        self.assertEqual(self.service.store.get_vm(vm.id).cpu_model, "Haswell-noTSX")

    def test_cpu_model_rejects_unlisted_qemu_model(self):
        store = VMManagerStore(self.catalog)
        vm = VMConfig(id="macos-studio", name="Studio", macos_version="tahoe", smbios=SMBIOSConfig())
        store.add_vm(vm)
        update = {key: value for key, value in self.draft(cpu_model="not-a-qemu-model").items() if key != "id"}
        with patch("core.runtime_inventory.inspect_runtime", return_value={"qemu_cpu_models": ["Skylake-Client-noTSX-IBRS"]}):
            with self.assertRaisesRegex(Exception, "não foi confirmado pelo QEMU"):
                self.service.handlers()["vms.config.update"]({
                    "vm_id": vm.id, "config": update, "expected_revision": f'"{store.revision}"',
                })
        self.assertEqual(self.service.store.get_vm(vm.id).cpu_model, "Skylake-Client-noTSX-IBRS")

    def test_update_persists_reims_resolution_hidpi_and_refresh(self):
        store = VMManagerStore(self.catalog)
        vm = VMConfig(id="macos-studio", name="Studio", macos_version="tahoe", smbios=SMBIOSConfig())
        store.add_vm(vm)
        update = {key: value for key, value in self.draft(
            display_resolution="3840x2160", display_hidpi=True, display_refresh_hz=120,
        ).items() if key != "id"}

        result = self.service.handlers()["vms.config.update"]({
            "vm_id": vm.id, "config": update, "expected_revision": f'"{store.revision}"',
        })

        self.assertEqual(result["config"]["display_resolution"], "3840x2160")
        self.assertTrue(result["config"]["display_hidpi"])
        self.assertEqual(result["config"]["display_refresh_hz"], 120)

    def test_rejects_hi_dpi_resolution_that_is_too_narrow(self):
        update = {key: value for key, value in self.draft(
            display_resolution="1280x720", display_hidpi=True,
        ).items() if key != "id"}
        with self.assertRaises(RpcRemoteError) as caught:
            self.service.update_vm_config({
                "vm_id": "macos-studio", "config": update,
                "expected_revision": f'"{self.service.store.revision}"',
            })
        self.assertEqual(caught.exception.code, "invalid_vm_config")

    def test_update_rejects_running_vm(self):
        store = VMManagerStore(self.catalog)
        store.add_vm(VMConfig(id="macos-studio", name="Studio", macos_version="tahoe", smbios=SMBIOSConfig()))
        service = WebAgentService(self.catalog, running_vms=lambda: {"macos-studio": 1234})
        update = {key: value for key, value in self.draft().items() if key != "id"}
        with self.assertRaises(RpcRemoteError) as caught:
            service.update_vm_config({
                "vm_id": "macos-studio", "config": update,
                "expected_revision": f'"{service.store.revision}"',
            })
        self.assertEqual(caught.exception.code, "vm_running")

    def test_update_can_round_trip_but_not_change_legacy_gpu_selection(self):
        store = VMManagerStore(self.catalog)
        vm = VMConfig(id="macos-studio", name="Studio", macos_version="tahoe", smbios=SMBIOSConfig())
        vm.selected_gpu = "0000:01:00.0"
        vm.selected_gpu_name = "Existing GPU"
        store.add_vm(vm)
        update = {key: value for key, value in self.draft(selected_gpu="0000:01:00.0").items() if key != "id"}
        current = WebAgentService(self.catalog, running_vms=lambda: {})
        result = current.update_vm_config({
            "vm_id": "macos-studio", "config": update,
            "expected_revision": f'"{current.store.revision}"',
        })
        self.assertEqual(result["config"]["selected_gpu"], "0000:01:00.0")

        changed = {**update, "selected_gpu": "0000:02:00.0"}
        with self.assertRaises(RpcRemoteError) as caught:
            current.update_vm_config({
                "vm_id": "macos-studio", "config": changed,
                "expected_revision": f'"{current.store.revision}"',
            })
        self.assertEqual(caught.exception.code, "gpu_selection_unavailable")

    def test_update_accepts_gpu_only_when_bdf_and_uuid_match_inventory(self):
        store = VMManagerStore(self.catalog)
        vm = VMConfig(id="macos-studio", name="Studio", macos_version="tahoe", smbios=SMBIOSConfig())
        store.add_vm(vm)
        current = WebAgentService(self.catalog, running_vms=lambda: {})
        current.gpu_inventory = lambda: [{
            "pci_bdf": "0000:02:00.0", "name": "Validated GPU", "vulkan_uuid": "gpu-uuid-2",
            "selection_supported": True,
        }]
        update = {key: value for key, value in self.draft(
            selected_gpu="0000:02:00.0", selected_gpu_uuid="gpu-uuid-2",
        ).items() if key != "id"}

        result = current.update_vm_config({
            "vm_id": vm.id, "config": update,
            "expected_revision": f'"{current.store.revision}"',
        })

        self.assertEqual(result["config"]["selected_gpu"], "0000:02:00.0")
        self.assertEqual(result["config"]["selected_gpu_uuid"], "gpu-uuid-2")
        self.assertEqual(result["config"]["selected_gpu_name"], "Validated GPU")

    def test_update_rejects_gpu_uuid_that_no_longer_matches_inventory(self):
        store = VMManagerStore(self.catalog)
        vm = VMConfig(id="macos-studio", name="Studio", macos_version="tahoe", smbios=SMBIOSConfig())
        store.add_vm(vm)
        current = WebAgentService(self.catalog, running_vms=lambda: {})
        current.gpu_inventory = lambda: [{
            "pci_bdf": "0000:02:00.0", "name": "Validated GPU", "vulkan_uuid": "current-uuid",
            "selection_supported": True,
        }]
        update = {key: value for key, value in self.draft(
            selected_gpu="0000:02:00.0", selected_gpu_uuid="stale-uuid",
        ).items() if key != "id"}

        with self.assertRaises(RpcRemoteError) as caught:
            current.update_vm_config({
                "vm_id": vm.id, "config": update,
                "expected_revision": f'"{current.store.revision}"',
            })

        self.assertEqual(caught.exception.code, "gpu_selection_unavailable")

    def test_stale_revision_is_conflict(self):
        revision = self.service.store.revision
        self.service.store.add_vm(VMConfig(id="other", name="Other", macos_version="tahoe"))
        with self.assertRaises(RpcRemoteError) as caught:
            self.service.create_vm_config({"config": self.draft(), "expected_revision": f'"{revision}"'})
        self.assertEqual(caught.exception.code, "catalog_conflict")

    def test_unsupported_gpu_and_unsafe_qemu_args_are_rejected(self):
        revision = self.service.store.revision
        for invalid, code in (
            ({"selected_gpu": "0000:01:00.0"}, "gpu_selection_unavailable"),
            ({"qemu_extra_args": ["-device", "evil"]}, "invalid_vm_config"),
        ):
            with self.subTest(code=code), self.assertRaises(RpcRemoteError) as caught:
                self.service.create_vm_config({
                    "config": self.draft(**invalid), "expected_revision": f'"{revision}"',
                })
            self.assertEqual(caught.exception.code, code)

    def test_network_configuration_and_unique_mac_are_persisted(self):
        revision = self.service.store.revision
        first = self.service.create_vm_config({
            "config": self.draft(network_enabled=False, network_mac="52:54:00:12:34:56"),
            "expected_revision": f'"{revision}"',
        })
        self.assertFalse(first["config"]["network_enabled"])
        self.assertEqual(first["config"]["network_mac"], "52:54:00:12:34:56")
        second = self.draft(id="macos-other", network_mac="52:54:00:12:34:56")
        with self.assertRaises(RpcRemoteError) as caught:
            self.service.create_vm_config({
                "config": second, "expected_revision": f'"{self.service.store.revision}"',
            })
        self.assertEqual(caught.exception.code, "network_mac_conflict")

    def test_ssh_forward_is_exposed_only_when_reims_supports_it_and_port_is_unique(self):
        runtime = {"status": "available", "reims_set_options": ["--ssh-port"]}
        with patch("core.runtime_inventory.inspect_runtime", return_value=runtime):
            first = self.service.create_vm_config({
                "config": self.draft(network_ssh_port=2222),
                "expected_revision": f'"{self.service.store.revision}"',
            })
            self.assertEqual(first["config"]["network_ssh_port"], 2222)

            with self.assertRaises(RpcRemoteError) as caught:
                self.service.create_vm_config({
                    "config": self.draft(id="macos-other", network_ssh_port=2222),
                    "expected_revision": f'"{self.service.store.revision}"',
                })
        self.assertEqual(caught.exception.code, "ssh_forward_port_conflict")

        with patch("core.runtime_inventory.inspect_runtime", return_value={"status": "available", "reims_set_options": []}):
            with self.assertRaises(RpcRemoteError) as unsupported:
                self.service.create_vm_config({
                    "config": self.draft(id="macos-third", network_ssh_port=2223),
                    "expected_revision": f'"{self.service.store.revision}"',
                })
        self.assertEqual(unsupported.exception.code, "ssh_forward_unavailable")

    def test_multiple_displays_require_reims_capability_and_can_be_edited(self):
        with patch("core.runtime_inventory.inspect_runtime", return_value={"status": "available", "reims_set_options": []}):
            with self.assertRaises(RpcRemoteError) as unsupported:
                self.service.create_vm_config({
                    "config": self.draft(display_count=2),
                    "expected_revision": f'"{self.service.store.revision}"',
                })
        self.assertEqual(unsupported.exception.code, "display_count_unavailable")

        with patch("core.runtime_inventory.inspect_runtime", return_value={"status": "available", "reims_set_options": ["--displays"]}):
            created = self.service.create_vm_config({
                "config": self.draft(display_count=2),
                "expected_revision": f'"{self.service.store.revision}"',
            })
            self.assertEqual(created["config"]["display_count"], 2)
            update = {key: value for key, value in self.draft(display_count=3).items() if key != "id"}
            changed = self.service.update_vm_config({
                "vm_id": "macos-studio", "config": update,
                "expected_revision": f'"{self.service.store.revision}"',
            })
        self.assertEqual(changed["config"]["display_count"], 3)

    def test_audio_backend_must_be_reported_by_selected_qemu(self):
        with patch("core.runtime_inventory.inspect_runtime", return_value={"qemu_audio_backends": ["pipewire", "none"]}):
            revision = self.service.store.revision
            created = self.service.create_vm_config({
                "config": self.draft(audio_backend="pipewire"),
                "expected_revision": f'"{revision}"',
            })
        self.assertEqual(created["config"]["audio_backend"], "pipewire")
        with patch("core.runtime_inventory.inspect_runtime", return_value={"qemu_audio_backends": ["alsa"]}):
            with self.assertRaises(RpcRemoteError) as caught:
                self.service.create_vm_config({
                    "config": self.draft(id="macos-other", audio_backend="pipewire"),
                    "expected_revision": f'"{self.service.store.revision}"',
                })
        self.assertEqual(caught.exception.code, "audio_backend_unavailable")

    def test_cpu_topology_requires_reims_feature_and_can_be_persisted_when_announced(self):
        unsupported = self.draft(cpu_topology={"sockets": 2, "cores": 3, "threads": 1})
        with patch("core.runtime_inventory.inspect_runtime", return_value={"reims_schema_features": []}):
            with self.assertRaises(RpcRemoteError) as caught:
                self.service.create_vm_config({
                    "config": unsupported, "expected_revision": f'"{self.service.store.revision}"',
                })
        self.assertEqual(caught.exception.code, "cpu_topology_unavailable")

        supported = self.draft(cpu_topology={"sockets": 2, "cores": 3, "threads": 1})
        with patch("core.runtime_inventory.inspect_runtime", return_value={"reims_schema_features": ["cpu_topology"]}):
            result = self.service.create_vm_config({
                "config": supported, "expected_revision": f'"{self.service.store.revision}"',
            })
        self.assertEqual(result["config"]["cpu_topology"], {"sockets": 2, "cores": 3, "threads": 1})

    def test_update_omitting_cpu_topology_preserves_legacy_value(self):
        from core.config import CpuTopologyConfig

        vm = VMConfig(
            id="macos-studio", name="Studio", macos_version="tahoe", vcpus=6,
            cpu_topology=CpuTopologyConfig(2, 3, 1), smbios=SMBIOSConfig(),
        )
        self.service.store.add_vm(vm)
        update = {key: value for key, value in self.draft(vcpus=6, name="Renamed").items() if key != "id"}
        with patch("core.runtime_inventory.inspect_runtime", return_value={"reims_schema_features": []}):
            result = self.service.update_vm_config({
                "vm_id": vm.id, "config": update,
                "expected_revision": f'"{self.service.store.revision}"',
            })
        self.assertEqual(result["config"]["cpu_topology"], {"sockets": 2, "cores": 3, "threads": 1})

    def test_audio_enable_flag_is_persisted_and_compared_with_reims_projection(self):
        store = VMManagerStore(self.catalog)
        vm = VMConfig(id="macos-audio", name="Audio", macos_version="tahoe", smbios=SMBIOSConfig())
        store.add_vm(vm)
        projection_dir = store.config_dir / "vms" / vm.id
        projection_dir.mkdir(parents=True)
        (projection_dir / "vm.toml").write_text('[audio]\nenabled = true\nbackend = "pipewire"\n', encoding="utf-8")
        update = {key: value for key, value in self.draft(audio_enabled=False).items() if key != "id"}
        result = self.service.handlers()["vms.config.update"]({
            "vm_id": vm.id, "config": update, "expected_revision": f'"{store.revision}"',
        })
        self.assertFalse(result["config"]["audio_enabled"])
        differences = {item["field"]: item for item in result["config_differences"]}
        self.assertEqual(differences["audio_enabled"]["saved_value"], "False")
        self.assertEqual(differences["audio_enabled"]["last_projection_value"], "True")

    def test_safe_qemu_arguments_persist_on_update(self):
        vm = VMConfig(id="macos-studio", name="Studio", macos_version="tahoe")
        self.service.store.add_vm(vm)
        update = {key: value for key, value in self.draft(
            qemu_extra_args=["-rtc", "base=utc", "-msg", "timestamp=on"],
        ).items() if key != "id"}
        result = self.service.update_vm_config({
            "vm_id": vm.id,
            "config": update,
            "expected_revision": f'"{self.service.store.revision}"',
        })
        self.assertEqual(result["config"]["qemu_extra_args"], update["qemu_extra_args"])
        self.assertEqual(self.service.store.get_vm(vm.id).qemu_extra_args, update["qemu_extra_args"])

    def test_tui_patch_is_allowlisted_revision_guarded_and_rejects_running_vm(self):
        store = VMManagerStore(self.catalog)
        store.add_vm(VMConfig(id="macos-studio", name="Studio", macos_version="tahoe"))
        service = WebAgentService(self.catalog, running_vms=lambda: {})
        revision = service.store.revision
        result = service.handlers()["vms.config.patch"]({
            "vm_id": "macos-studio", "expected_revision": revision,
            "fields": {"usb_auto_attach": ["1050:0407"], "install_phase": "installing", "install_reboots": 2},
        })
        self.assertEqual(result["config"]["usb_auto_attach"], ["1050:0407"])
        self.assertEqual(result["config"]["install_phase"], "installing")
        self.assertEqual(result["config"]["install_reboots"], 2)

        with self.assertRaises(RpcRemoteError) as blocked:
            service.handlers()["vms.config.patch"]({
                "vm_id": "macos-studio", "expected_revision": result["revision"],
                "fields": {"disk_path": "/tmp/other-disk.qcow2"},
            })
        self.assertEqual(blocked.exception.code, "invalid_vm_patch")

        running = WebAgentService(self.catalog, running_vms=lambda: {"macos-studio": 1234})
        with self.assertRaises(RpcRemoteError) as conflict:
            running.patch_vm_config({
                "vm_id": "macos-studio", "expected_revision": running.store.revision,
                "fields": {"install_phase": "installed"},
            })
        self.assertEqual(conflict.exception.code, "vm_running")

    def test_delete_is_agent_owned_cas_guarded_and_limits_disk_removal_to_managed_vm_dir(self):
        store = VMManagerStore(self.catalog)
        vm_dir = store.config_dir / "vms" / "macos-studio"
        vm_dir.mkdir(parents=True)
        disk = vm_dir / "macos.qcow2"
        disk.write_bytes(b"disk")
        store.add_vm(VMConfig(id="macos-studio", name="Studio", macos_version="tahoe", disk_path=str(disk)))
        service = WebAgentService(self.catalog, running_vms=lambda: {})
        revision = service.store.revision
        result = service.handlers()["vms.config.delete"]({
            "vm_id": "macos-studio", "expected_revision": revision, "delete_disk": True,
        })
        self.assertTrue(result["deleted"])
        self.assertFalse(disk.exists())
        self.assertIsNone(service.store.get_vm("macos-studio"))

        external = self.catalog.parent / "external.qcow2"
        external.write_bytes(b"keep")
        store = VMManagerStore(self.catalog)
        store.add_vm(VMConfig(id="outside-vm", name="Outside", macos_version="tahoe", disk_path=str(external)))
        service = WebAgentService(self.catalog, running_vms=lambda: {})
        with self.assertRaises(RpcRemoteError) as denied:
            service.delete_vm_config({
                "vm_id": "outside-vm", "expected_revision": service.store.revision, "delete_disk": True,
            })
        self.assertEqual(denied.exception.code, "disk_delete_forbidden")
        self.assertTrue(external.is_file())

    def test_delete_preserves_a_disk_referenced_by_another_catalog_vm(self):
        store = VMManagerStore(self.catalog)
        vm_dir = store.config_dir / "vms" / "macos-studio"
        vm_dir.mkdir(parents=True)
        disk = vm_dir / "macos.qcow2"
        disk.write_bytes(b"shared disk")
        store.add_vm(VMConfig(id="macos-studio", name="Studio", macos_version="tahoe", disk_path=str(disk)))
        store.add_vm(VMConfig(id="macos-test", name="Test", macos_version="tahoe", disk_path=str(disk)))
        service = WebAgentService(self.catalog, running_vms=lambda: {})

        with self.assertRaises(RpcRemoteError) as denied:
            service.delete_vm_config({
                "vm_id": "macos-studio", "expected_revision": service.store.revision, "delete_disk": True,
            })

        self.assertEqual(denied.exception.code, "disk_shared")
        self.assertTrue(disk.is_file())
        self.assertIsNotNone(service.store.get_vm("macos-studio"))
        self.assertIsNotNone(service.store.get_vm("macos-test"))

    def test_store_delete_atomically_rechecks_shared_disk_references(self):
        store = VMManagerStore(self.catalog)
        vm_dir = store.config_dir / "vms" / "macos-studio"
        vm_dir.mkdir(parents=True)
        disk = vm_dir / "macos.qcow2"
        disk.write_bytes(b"shared disk")
        store.add_vm(VMConfig(id="macos-studio", name="Studio", macos_version="tahoe", disk_path=str(disk)))
        # The store-level check is the final guard under the catalog lock; callers
        # cannot bypass it by racing the service's earlier preflight check.
        store.add_vm(VMConfig(id="macos-test", name="Test", macos_version="tahoe", disk_path=str(disk)))

        with self.assertRaisesRegex(RuntimeError, "associado à VM Test"):
            store.delete_vm("macos-studio", delete_disk=True)

        self.assertTrue(disk.is_file())
        self.assertIsNotNone(store.get_vm("macos-studio"))
        self.assertIsNotNone(store.get_vm("macos-test"))

    @unittest.skipUnless(shutil.which("qemu-img"), "qemu-img is required to test backing-chain protection")
    def test_delete_preserves_backing_image_used_by_another_vm(self):
        store = VMManagerStore(self.catalog)
        target_dir = store.config_dir / "vms" / "macos-studio"
        other_dir = store.config_dir / "vms" / "macos-test"
        target_dir.mkdir(parents=True)
        other_dir.mkdir(parents=True)
        base = target_dir / "macos.qcow2"
        overlay = other_dir / "macos.qcow2"
        subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", str(base), "1M"], check=True)
        subprocess.run([
            "qemu-img", "create", "-q", "-f", "qcow2", "-F", "qcow2", "-b", str(base), str(overlay), "1M",
        ], check=True)
        store.add_vm(VMConfig(id="macos-studio", name="Studio", macos_version="tahoe", disk_path=str(base)))
        store.add_vm(VMConfig(id="macos-test", name="Test", macos_version="tahoe", disk_path=str(overlay)))
        service = WebAgentService(self.catalog, running_vms=lambda: {})

        with self.assertRaises(RpcRemoteError) as denied:
            service.delete_vm_config({
                "vm_id": "macos-studio", "expected_revision": service.store.revision, "delete_disk": True,
            })

        self.assertEqual(denied.exception.code, "disk_shared")
        self.assertTrue(base.is_file())
        self.assertTrue(overlay.is_file())

    def test_catalog_save_failure_restores_staged_disk_and_vm(self):
        store = VMManagerStore(self.catalog)
        vm_dir = store.config_dir / "vms" / "macos-studio"
        vm_dir.mkdir(parents=True)
        disk = vm_dir / "macos.qcow2"
        disk.write_bytes(b"keep on catalog failure")
        vm = VMConfig(id="macos-studio", name="Studio", macos_version="tahoe", disk_path=str(disk))
        store.add_vm(vm)

        with patch.object(store, "_save_locked", side_effect=OSError("catalog unavailable")):
            with self.assertRaisesRegex(OSError, "catalog unavailable"):
                store.delete_vm(vm.id, delete_disk=True)

        self.assertTrue(disk.is_file())
        self.assertEqual(disk.read_bytes(), b"keep on catalog failure")
        self.assertIsNotNone(store.get_vm(vm.id))
        self.assertEqual(list(vm_dir.glob(".*.delete-*")), [])

    def test_disk_unlink_failure_keeps_vm_and_restores_disk(self):
        store = VMManagerStore(self.catalog)
        vm_dir = store.config_dir / "vms" / "macos-studio"
        vm_dir.mkdir(parents=True)
        disk = vm_dir / "macos.qcow2"
        disk.write_bytes(b"keep on unlink failure")
        vm = VMConfig(id="macos-studio", name="Studio", macos_version="tahoe", disk_path=str(disk))
        store.add_vm(vm)

        original_unlink = Path.unlink

        def reject_only_staged_disk(path, *args, **kwargs):
            if ".delete-" in path.name:
                raise PermissionError("disk is busy")
            return original_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", reject_only_staged_disk):
            with self.assertRaisesRegex(PermissionError, "disk is busy"):
                store.delete_vm(vm.id, delete_disk=True)

        self.assertTrue(disk.is_file())
        self.assertEqual(disk.read_bytes(), b"keep on unlink failure")
        self.assertIsNotNone(store.get_vm(vm.id))
        self.assertEqual(list(vm_dir.glob(".*.delete-*")), [])
        self.assertEqual(list(store.config_dir.glob(".disk-delete-*.json")), [])

    def test_startup_recovers_disk_if_process_stopped_before_catalog_commit(self):
        store = VMManagerStore(self.catalog)
        vm_dir = store.config_dir / "vms" / "macos-studio"
        vm_dir.mkdir(parents=True)
        disk = vm_dir / "macos.qcow2"
        disk.write_bytes(b"restore after interrupted transaction")
        vm = VMConfig(id="macos-studio", name="Studio", macos_version="tahoe", disk_path=str(disk))
        store.add_vm(vm)

        with patch.object(store, "_save_locked", side_effect=SystemExit("simulated process stop")):
            with self.assertRaisesRegex(SystemExit, "simulated process stop"):
                store.delete_vm(vm.id, delete_disk=True)
        self.assertFalse(disk.exists())
        self.assertEqual(len(list(store.config_dir.glob(".disk-delete-*.json"))), 1)

        recovered = VMManagerStore(self.catalog)
        self.assertTrue(disk.is_file())
        self.assertEqual(disk.read_bytes(), b"restore after interrupted transaction")
        self.assertIsNotNone(recovered.get_vm(vm.id))
        self.assertEqual(list(store.config_dir.glob(".disk-delete-*.json")), [])

    def test_startup_cleans_staged_disk_after_catalog_commit(self):
        store = VMManagerStore(self.catalog)
        vm_dir = store.config_dir / "vms" / "macos-studio"
        vm_dir.mkdir(parents=True)
        disk = vm_dir / "macos.qcow2"
        disk.write_bytes(b"remove after committed delete")
        vm = VMConfig(id="macos-studio", name="Studio", macos_version="tahoe", disk_path=str(disk))
        store.add_vm(vm)
        original_unlink = Path.unlink

        def stop_before_staged_unlink(path, *args, **kwargs):
            if ".delete-" in path.name:
                raise SystemExit("simulated process stop")
            return original_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", stop_before_staged_unlink):
            with self.assertRaisesRegex(SystemExit, "simulated process stop"):
                store.delete_vm(vm.id, delete_disk=True)
        self.assertIsNone(store.get_vm(vm.id))
        self.assertFalse(disk.exists())
        self.assertEqual(len(list(vm_dir.glob(".*.delete-*"))), 1)

        recovered = VMManagerStore(self.catalog)
        self.assertIsNone(recovered.get_vm(vm.id))
        self.assertEqual(list(vm_dir.glob(".*.delete-*")), [])
        self.assertEqual(list(store.config_dir.glob(".disk-delete-*.json")), [])

    def test_invalid_macos_and_capacity_are_rejected(self):
        with self.assertRaises(RpcRemoteError) as caught:
            self.service.create_vm_config({
                "config": self.draft(macos_version="unknown"),
                "expected_revision": f'"{self.service.store.revision}"',
            })
        self.assertEqual(caught.exception.code, "invalid_vm_config")
        with self.assertRaises(RpcRemoteError) as caught:
            self.service.create_vm_config({
                "config": self.draft(ram_gb=32),
                "expected_revision": f'"{self.service.store.revision}"',
            })
        self.assertEqual(caught.exception.code, "invalid_vm_config")

    def test_update_rejects_picker_change_until_opencore_regeneration_exists(self):
        store = VMManagerStore(self.catalog)
        vm = VMConfig(
            id="macos-studio", name="Studio", macos_version="tahoe",
            smbios=SMBIOSConfig(), opencore_show_picker=False,
        )
        store.add_vm(vm)
        service = WebAgentService(self.catalog, running_vms=lambda: {})
        update = {key: value for key, value in self.draft(opencore_show_picker=True).items() if key != "id"}
        with self.assertRaises(RpcRemoteError) as caught:
            service.update_vm_config({
                "vm_id": vm.id, "config": update,
                "expected_revision": f'"{service.store.revision}"',
            })
        self.assertEqual(caught.exception.code, "opencore_regeneration_required")

    def test_picker_update_is_versioned_and_queued_as_vm_scoped_job(self):
        vm = VMConfig(id="macos-studio", name="Studio", macos_version="tahoe", opencore_show_picker=False)
        self.service.store.add_vm(vm)

        class Runner:
            def submit(self, **kwargs):
                self.kwargs = kwargs
                return {"id": "picker-job"}, True

        runner = Runner()
        self.service.job_runner = runner
        with patch("core.runtime_inventory.inspect_runtime", return_value=_runtime()):
            result = self.service.submit_vm_opencore_picker({
                "vm_id": vm.id,
                "boot_selection": True,
                "confirmation": vm.id,
                "idempotency_key": "00000000-0000-4000-8000-000000000001",
                "expected_revision": f'"{self.service.store.revision}"',
            })
        self.assertEqual(result["job_id"], "picker-job")
        self.assertEqual(runner.kwargs["kind"], "vm.opencore.picker")
        self.assertEqual(runner.kwargs["target_vm_id"], vm.id)
        self.assertTrue(runner.kwargs["payload"]["boot_selection"])

    def test_picker_regeneration_uses_fixed_reims_command_and_persists_observed_choice(self):
        from types import SimpleNamespace

        vm = VMConfig(id="macos-studio", name="Studio", macos_version="tahoe", opencore_show_picker=False)
        self.service.store.add_vm(vm)
        vm_dir = self.service.store.config_dir / "vms" / vm.id
        vm_dir.mkdir(parents=True)
        (vm_dir / "vm.toml").write_text('[q35]\nopencore = "OpenCore.qcow2"\n', encoding="utf-8")
        (vm_dir / "OpenCore.qcow2").write_bytes(b"old OpenCore image")
        expected = self.service.store.revision
        phases = []
        checkpoints = []

        class Context:
            @staticmethod
            def phase(name, progress=None):
                phases.append((name, progress))

            @staticmethod
            def checkpoint(name, payload=None, **_kwargs):
                checkpoints.append((name, payload))

            @staticmethod
            def check_cancelled():
                return None

        class Lock:
            def acquire(self, *_args, **_kwargs):
                return True

            def release(self):
                pass

        with patch("core.reims_ctl.vm_record", return_value={"name": vm.id, "dir": str(vm_dir), "state": "stopped", "pid": None}), \
             patch("core.reims_ctl.reims_ctl_bin", return_value="/managed/reims-ctl"), \
             patch("core.runtime_inventory.inspect_runtime", return_value=_runtime()), \
             patch("core.web_agent.shutil.which", return_value="/usr/bin/guestfish"), \
             patch.object(self.service, "_opencore_enabled_kexts", side_effect=[_kexts(LEGACY), _kexts(LEGACY)]) as verify, \
             patch("core.config.ExclusiveVMLock", Lock), \
             patch("subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="serial=PRIVATE", stderr="")) as run:
            result = self.service._run_vm_opencore_picker_job({
                "vm_id": vm.id, "boot_selection": True, "expected_revision": expected,
            }, Context())

        self.assertEqual(result, {"vm_id": vm.id, "boot_selection": True, "message": "OpenCore regenerado com a identidade SMBIOS preservada."})
        self.assertEqual(verify.call_count, 2)
        self.assertEqual(run.call_args.args[0], ["/managed/reims-ctl", "update-opencore", vm.id, "--boot-selection", "true"])
        self.assertEqual(run.call_args.kwargs["env"]["REIMS_CTL_UTIL"], "/managed/reims-util")
        self.assertNotIn("PRIVATE", str(result))
        self.assertTrue(self.service.store.get_vm(vm.id).opencore_show_picker)
        self.assertEqual(checkpoints[0][0], "opencore_regeneration_started")
        self.assertEqual(phases[-1], ("OpenCore atualizado", 100))

    def test_picker_regeneration_fails_if_successful_reims_leaves_only_one_kext(self):
        """rc=0 is not success if the kext set shrank, regardless of kext names."""
        self._assert_picker_kext_shrink_fails()

    def _assert_picker_kext_shrink_fails(self):
        import plistlib
        from types import SimpleNamespace

        vm = VMConfig(id="macos-studio", name="Studio", macos_version="tahoe", opencore_show_picker=False)
        self.service.store.add_vm(vm)
        vm_dir = self.service.store.config_dir / "vms" / vm.id
        vm_dir.mkdir(parents=True)
        (vm_dir / "vm.toml").write_text('[q35]\nopencore = "OpenCore.qcow2"\n', encoding="utf-8")
        image = vm_dir / "OpenCore.qcow2"
        image.write_bytes(b"old image")
        expected = self.service.store.revision

        class Context:
            @staticmethod
            def phase(*_args, **_kwargs):
                return None

            @staticmethod
            def checkpoint(*_args, **_kwargs):
                return None

            @staticmethod
            def check_cancelled():
                return None

        class Lock:
            def acquire(self, *_args, **_kwargs):
                return True

            def release(self):
                return None

        seen = {"n": 0}

        def fake_run(command, **_kwargs):
            if command[:2] == ["/managed/reims-ctl", "update-opencore"]:
                image.write_bytes(b"EFI with only Lilu")
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if command[0] == "guestfish" and "download" in command:
                seen["n"] += 1
                bundles = _kexts(LEGACY) if seen["n"] == 1 else {"Lilu.kext"}
                plist = {"Kernel": {"Add": [{"BundlePath": name, "Enabled": True} for name in sorted(bundles)]}}
                Path(command[-1]).write_bytes(plistlib.dumps(plist))
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if command[0] == "guestfish" and "ls" in command:
                bundles = _kexts(LEGACY) if seen["n"] == 1 else {"Lilu.kext"}
                return SimpleNamespace(returncode=0, stdout="\n".join(sorted(bundles)) + "\n", stderr="")
            raise AssertionError(f"comando inesperado: {command}")

        with patch("core.reims_ctl.vm_record", return_value={"name": vm.id, "dir": str(vm_dir), "state": "stopped", "pid": None}), \
             patch("core.reims_ctl.reims_ctl_bin", return_value="/managed/reims-ctl"), \
             patch("core.runtime_inventory.inspect_runtime", return_value=_runtime()), \
             patch("core.web_agent.shutil.which", return_value="/usr/bin/guestfish"), \
             patch("core.config.ExclusiveVMLock", Lock), \
             patch("subprocess.run", side_effect=fake_run) as run:
            with self.assertRaisesRegex(RuntimeError, "não corresponde exatamente ao perfil"):
                self.service._run_vm_opencore_picker_job({
                    "vm_id": vm.id, "boot_selection": True, "expected_revision": expected,
                }, Context())

        commands = [call.args[0] for call in run.call_args_list]
        update_index = next(i for i, command in enumerate(commands) if command[:2] == ["/managed/reims-ctl", "update-opencore"])
        # The pre-mutation snapshot must be read before the mutation runs.
        self.assertTrue(all(command[0] == "guestfish" for command in commands[:update_index]))
        self.assertNotEqual(image.read_bytes(), b"EFI with only Lilu")
        self.assertFalse(self.service.store.get_vm(vm.id).opencore_show_picker)

    def test_picker_regeneration_refuses_add_kext_mode_before_starting_reims(self):
        """An add-kext util needs a caller that passes --add-kext; this ctl does not."""
        vm = VMConfig(id="macos-studio", name="Studio", macos_version="tahoe", opencore_show_picker=False)
        self.service.store.add_vm(vm)
        vm_dir = self.service.store.config_dir / "vms" / vm.id
        vm_dir.mkdir(parents=True)
        (vm_dir / "vm.toml").write_text('[q35]\nopencore = "OpenCore.qcow2"\n', encoding="utf-8")
        (vm_dir / "OpenCore.qcow2").write_bytes(b"original image")

        class Context:
            @staticmethod
            def check_cancelled():
                return None

        with patch("core.reims_ctl.vm_record", return_value={"name": vm.id, "dir": str(vm_dir), "state": "stopped", "pid": None}), \
             patch("core.runtime_inventory.inspect_runtime", return_value=_runtime(kext_mode="add-kext")), \
             patch("subprocess.run") as run:
            with self.assertRaisesRegex(RuntimeError, "sem kexts embutidos|opera no modo"):
                self.service._run_vm_opencore_picker_job({
                    "vm_id": vm.id, "boot_selection": True, "expected_revision": self.service.store.revision,
                }, Context())
        run.assert_not_called()
        self.assertEqual((vm_dir / "OpenCore.qcow2").read_bytes(), b"original image")
        self.assertFalse(self.service.store.get_vm(vm.id).opencore_show_picker)

    def test_picker_regeneration_leaves_no_orphan_backup_when_before_read_fails(self):
        """A `before` inspection failure mutates nothing, so no backup must linger."""
        vm = VMConfig(id="macos-studio", name="Studio", macos_version="tahoe", opencore_show_picker=False)
        self.service.store.add_vm(vm)
        vm_dir = self.service.store.config_dir / "vms" / vm.id
        vm_dir.mkdir(parents=True)
        (vm_dir / "vm.toml").write_text('[q35]\nopencore = "OpenCore.qcow2"\n', encoding="utf-8")
        (vm_dir / "OpenCore.qcow2").write_bytes(b"original image")

        class Context:
            @staticmethod
            def check_cancelled(): return None
            @staticmethod
            def phase(*args, **kwargs): return None
            @staticmethod
            def checkpoint(*args, **kwargs): return None
            @staticmethod
            def log(*args, **kwargs): return None

        class Lock:
            def __init__(self, *args, **kwargs): pass
            def acquire(self, *args, **kwargs): return True
            def release(self): pass

        with patch("core.reims_ctl.vm_record", return_value={"name": vm.id, "dir": str(vm_dir), "state": "stopped", "pid": None}), \
             patch("core.runtime_inventory.inspect_runtime", return_value=_runtime()), \
             patch("core.web_agent.shutil.which", return_value=None), \
             patch("core.config.ExclusiveVMLock", Lock), \
             patch("subprocess.run") as run:
            with self.assertRaisesRegex(RuntimeError, "Nada foi alterado|nada foi alterado"):
                self.service._run_vm_opencore_picker_job({
                    "vm_id": vm.id, "boot_selection": True, "expected_revision": self.service.store.revision,
                }, Context())
        run.assert_not_called()
        self.assertEqual((vm_dir / "OpenCore.qcow2").read_bytes(), b"original image")
        self.assertEqual({entry.name for entry in vm_dir.iterdir()}, {"vm.toml", "OpenCore.qcow2"})

    def test_picker_regeneration_refuses_external_or_linked_opencore_before_starting_reims(self):
        vm = VMConfig(id="macos-studio", name="Studio", macos_version="tahoe")
        self.service.store.add_vm(vm)
        vm_dir = self.service.store.config_dir / "vms" / vm.id
        vm_dir.mkdir(parents=True)
        (vm_dir / "vm.toml").write_text('[q35]\nopencore = "OpenCore.qcow2"\n', encoding="utf-8")
        outside = Path(self.temp.name) / "outside.qcow2"
        outside.write_bytes(b"keep")
        (vm_dir / "OpenCore.qcow2").symlink_to(outside)

        class Context:
            @staticmethod
            def check_cancelled():
                return None

        with patch("core.reims_ctl.vm_record", return_value={"name": vm.id, "dir": str(vm_dir), "state": "stopped", "pid": None}), \
             patch("core.runtime_inventory.inspect_runtime", return_value=_runtime()), \
             patch("subprocess.run") as run:
            with self.assertRaisesRegex(RuntimeError, "arquivo gerenciado"):
                self.service._run_vm_opencore_picker_job({
                    "vm_id": vm.id, "boot_selection": True, "expected_revision": self.service.store.revision,
                }, Context())
        run.assert_not_called()
        self.assertEqual(outside.read_bytes(), b"keep")

    def test_picker_regeneration_refuses_opencore_path_escaping_through_parent_symlink(self):
        vm = VMConfig(id="macos-studio", name="Studio", macos_version="tahoe")
        self.service.store.add_vm(vm)
        vm_dir = self.service.store.config_dir / "vms" / vm.id
        vm_dir.mkdir(parents=True)
        outside_dir = Path(self.temp.name) / "outside-dir"
        outside_dir.mkdir()
        (outside_dir / "OpenCore.qcow2").write_bytes(b"keep")
        (vm_dir / "links").symlink_to(outside_dir, target_is_directory=True)
        (vm_dir / "vm.toml").write_text('[q35]\nopencore = "links/OpenCore.qcow2"\n', encoding="utf-8")

        class Context:
            @staticmethod
            def check_cancelled():
                return None

        with patch("core.reims_ctl.vm_record", return_value={"name": vm.id, "dir": str(vm_dir), "state": "stopped", "pid": None}), \
             patch("core.runtime_inventory.inspect_runtime", return_value=_runtime()), \
             patch("subprocess.run") as run:
            with self.assertRaisesRegex(RuntimeError, "arquivo gerenciado"):
                self.service._run_vm_opencore_picker_job({
                    "vm_id": vm.id, "boot_selection": True, "expected_revision": self.service.store.revision,
                }, Context())
        run.assert_not_called()
        self.assertEqual((outside_dir / "OpenCore.qcow2").read_bytes(), b"keep")

    def test_smbios_diagnostic_is_versioned_and_queues_for_managed_vm(self):
        vm = VMConfig(id="macos-studio", name="Studio", macos_version="tahoe")
        self.service.store.add_vm(vm)

        class Runner:
            def submit(self, **kwargs):
                self.kwargs = kwargs
                return {"id": "smbios-job"}, True

        runner = Runner()
        self.service.job_runner = runner
        with patch("core.web_agent.shutil.which", return_value="/usr/bin/guestfish"):
            result = self.service.submit_vm_smbios_diagnostic({
                "vm_id": vm.id,
                "idempotency_key": "00000000-0000-4000-8000-000000000002",
                "expected_revision": f'"{self.service.store.revision}"',
            })
        self.assertEqual(result["job_id"], "smbios-job")
        self.assertEqual(runner.kwargs["kind"], "vm.smbios.diagnose")
        self.assertEqual(runner.kwargs["target_vm_id"], vm.id)
        self.assertEqual(runner.kwargs["payload"]["expected_revision"], self.service.store.revision)

    def test_smbios_diagnostic_runner_redacts_identity_and_uses_read_only_inspector(self):
        vm = VMConfig(
            id="macos-studio", name="Studio", macos_version="tahoe",
            smbios=SMBIOSConfig(model="iMac20,1", serial_number="SECRET-SERIAL", board_serial="SECRET-MLB", smuuid="11111111-2222-3333-4444-555555555555", rom="020000000001"),
        )
        self.service.store.add_vm(vm)
        vm_dir = self.service.store.config_dir / "vms" / vm.id
        vm_dir.mkdir(parents=True)
        (vm_dir / "vm.toml").write_text('[q35]\nopencore = "OpenCore.qcow2"\n', encoding="utf-8")
        image = vm_dir / "OpenCore.qcow2"
        image.write_bytes(b"managed image")
        checkpoints = []

        class Context:
            @staticmethod
            def phase(_name, _progress=None):
                pass

            @staticmethod
            def checkpoint(name, payload=None, **_kwargs):
                checkpoints.append((name, payload))

            @staticmethod
            def check_cancelled():
                return None

        class Lock:
            def acquire(self, *_args, **_kwargs):
                return True

            def release(self):
                pass

        with patch("core.reims_ctl.vm_record", return_value={"dir": str(vm_dir), "state": "stopped", "pid": None}), \
             patch("core.config.ExclusiveVMLock", Lock), \
             patch("core.smbios.GenSMBIOS.inspect_vm", return_value={"ok": True, "reason": "local SMBIOS fields match"}) as inspect:
            result = self.service._run_vm_smbios_diagnostic_job({
                "vm_id": vm.id, "expected_revision": self.service.store.revision,
            }, Context())
        inspect.assert_called_once_with(vm, image)
        self.assertEqual(result["status"], "match")
        self.assertNotIn("SECRET", str(result))
        self.assertEqual(checkpoints[0][0], "smbios_diagnostic_result")
        self.assertNotIn("SECRET", str(checkpoints))


if __name__ == "__main__":
    unittest.main()
