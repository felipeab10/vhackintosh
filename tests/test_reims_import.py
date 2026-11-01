from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.reims_import import ReimsImportError, build_vm_config, list_import_candidates
from core.reims_ctl import registry_file, vm_runtime_snapshot
from core.web_agent import WebAgentService


class ReimsImportTests(unittest.TestCase):
    def create_registered_vm(self, root: Path, *, name: str = "legacy-vm") -> tuple[Path, Path]:
        directory = root / "external" / name
        directory.mkdir(parents=True)
        with (directory / "system.raw").open("wb") as disk:
            disk.truncate(70 * 1024**3)
        (directory / "OpenCore.qcow2").write_bytes(b"efi disk")
        (directory / "OVMF_CODE.fd").write_bytes(b"firmware")
        (directory / "OVMF_VARS.fd").write_bytes(b"vars")
        (directory / "vm.toml").write_text(f'''\
name = "{name}"
profile = "q35-x86_64"
cpus = 6
memory_mib = 12288
disk = "system.raw"
disk_format = "raw"
extra_args = ["-action", "reboot=shutdown", "-rtc", "base=utc"]

[display]
count = 2
resolution = "2560x1440"
hidpi = true
borderless = true
refresh = 60

[network]
enabled = true
mac = "52:54:00:11:22:33"
ssh_port = 2222

[q35]
opencore = "OpenCore.qcow2"
opencore_format = "qcow2"
firmware_code = "OVMF_CODE.fd"
firmware_vars = "OVMF_VARS.fd"
cpu_model = "Skylake-Client-noTSX-IBRS"
''', encoding="utf-8")
        registry = root / "vms.toml"
        registry.write_text(
            f'[[vm]]\nname = "{name}"\ndir = {json.dumps(str(directory))}\n',
            encoding="utf-8",
        )
        return registry, directory

    def test_candidate_review_and_import_preserve_external_vm_identity_and_options(self):
        with tempfile.TemporaryDirectory() as temp:
            registry, directory = self.create_registered_vm(Path(temp))
            candidates = list_import_candidates(registry, set())
            self.assertEqual(len(candidates), 1)
            candidate = candidates[0]
            self.assertTrue(candidate["importable"])
            self.assertEqual(candidate["directory"], str(directory))
            self.assertEqual(candidate["disk_path"], str(directory / "system.raw"))
            self.assertEqual(candidate["disk_size_gb"], 70)
            vm = build_vm_config(candidate, "tahoe")

            self.assertEqual(vm.id, "legacy-vm")
            self.assertEqual(vm.disk_path, str(directory / "system.raw"))
            self.assertEqual(vm.qemu_extra_args, ["-rtc", "base=utc"])
            self.assertEqual(vm.display_resolution, "2560x1440")
            self.assertTrue(vm.display_hidpi)
            self.assertTrue(vm.fullscreen)
            self.assertEqual(vm.display_refresh_hz, 60)
            self.assertEqual(vm.display_count, 2)
            self.assertEqual(vm.network_ssh_port, 2222)
            self.assertEqual(vm.install_phase, "pending")
            self.assertEqual(vm.power_sync_mode, "vm-only")
            self.assertTrue((directory / "OpenCore.qcow2").is_file())
            self.assertTrue((directory / "OVMF_VARS.fd").is_file())

    def test_existing_catalog_entry_is_previewed_but_not_importable(self):
        with tempfile.TemporaryDirectory() as temp:
            registry, _ = self.create_registered_vm(Path(temp))
            candidate = list_import_candidates(registry, {"legacy-vm"})[0]
            self.assertFalse(candidate["importable"])
            self.assertIn("já está no catálogo", candidate["limitation"])

    def test_symlink_disk_and_unapproved_qemu_argument_are_blocked(self):
        with tempfile.TemporaryDirectory() as temp:
            registry, directory = self.create_registered_vm(Path(temp))
            outside = Path(temp) / "outside.raw"
            outside.write_bytes(b"external")
            (directory / "system.raw").unlink()
            (directory / "system.raw").symlink_to(outside)
            candidate = list_import_candidates(registry, set())[0]
            self.assertFalse(candidate["importable"])
            self.assertIn("link simbólico", candidate["limitation"])

        with tempfile.TemporaryDirectory() as temp:
            registry, directory = self.create_registered_vm(Path(temp))
            config = (directory / "vm.toml").read_text(encoding="utf-8").replace(
                '"-rtc", "base=utc"', '"-device", "unsafe"',
            )
            (directory / "vm.toml").write_text(config, encoding="utf-8")
            candidate = list_import_candidates(registry, set())[0]
            self.assertFalse(candidate["importable"])
            self.assertIn("argumentos QEMU", candidate["limitation"])

    def test_import_rechecks_files_after_review(self):
        with tempfile.TemporaryDirectory() as temp:
            registry, directory = self.create_registered_vm(Path(temp))
            candidate = list_import_candidates(registry, set())[0]
            (directory / "system.raw").unlink()
            with self.assertRaisesRegex(ReimsImportError, "mudou desde a prévia"):
                build_vm_config(candidate, "tahoe")

    def test_agent_import_adds_reviewed_vm_without_touching_external_efi_or_registry(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            registry, directory = self.create_registered_vm(root)
            original_registry = registry.read_bytes()
            original_efi = (directory / "OpenCore.qcow2").read_bytes()
            service = WebAgentService(root / "catalog.json", running_vms=lambda: {})
            expected = service.store.revision
            with patch("core.reims_ctl.registry_file", return_value=registry), patch(
                "core.reims_ctl.vm_runtime_snapshot", return_value={},
            ):
                candidate_list = service.handlers()["vms.import.candidates"]({})
                result = service.handlers()["vms.import"]({
                    "id": "legacy-vm",
                    "macos_version": "tahoe",
                    "expected_revision": f'"{expected}"',
                })

            self.assertEqual(candidate_list["items"][0]["id"], "legacy-vm")
            self.assertEqual(result["config"]["disk_path"], str(directory / "system.raw"))
            self.assertEqual(result["config"]["install_phase"], "pending")
            self.assertEqual(registry.read_bytes(), original_registry)
            self.assertEqual((directory / "OpenCore.qcow2").read_bytes(), original_efi)

    def test_running_legacy_vm_is_observed_and_cannot_be_imported(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            registry, directory = self.create_registered_vm(root)
            before = {
                "registry": registry.read_bytes(),
                "vm.toml": (directory / "vm.toml").read_bytes(),
                "disk": (directory / "system.raw").stat().st_size,
                "opencore": (directory / "OpenCore.qcow2").read_bytes(),
                "vars": (directory / "OVMF_VARS.fd").read_bytes(),
            }
            service = WebAgentService(root / "catalog.json", running_vms=lambda: {})
            with patch("core.reims_ctl.registry_file", return_value=registry), patch(
                "core.reims_ctl.vm_runtime_snapshot", return_value={"legacy-vm": 2345},
            ):
                candidate = service.handlers()["vms.import.candidates"]({})["items"][0]
                self.assertEqual(candidate["runtime_state"], "running")
                self.assertEqual(candidate["runtime_pid"], 2345)
                self.assertFalse(candidate["importable"])
                self.assertIsNone(candidate["limitation"])

                with self.assertRaises(Exception) as error:
                    service.handlers()["vms.import"]({
                        "id": "legacy-vm", "macos_version": "tahoe",
                        "expected_revision": f'"{service.store.revision}"',
                    })
            self.assertIn("em execução", str(error.exception).lower())
            self.assertIsNone(service.store.get_vm("legacy-vm"))
            self.assertEqual(registry.read_bytes(), before["registry"])
            self.assertEqual((directory / "vm.toml").read_bytes(), before["vm.toml"])
            self.assertEqual((directory / "system.raw").stat().st_size, before["disk"])
            self.assertEqual((directory / "OpenCore.qcow2").read_bytes(), before["opencore"])
            self.assertEqual((directory / "OVMF_VARS.fd").read_bytes(), before["vars"])

    def test_unknown_runtime_state_blocks_legacy_import(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            registry, _ = self.create_registered_vm(root)
            service = WebAgentService(root / "catalog.json", running_vms=lambda: {})
            with patch("core.reims_ctl.registry_file", return_value=registry), patch(
                "core.reims_ctl.vm_runtime_snapshot", return_value=None,
            ):
                candidate = service.handlers()["vms.import.candidates"]({})["items"][0]
                self.assertEqual(candidate["runtime_state"], "unknown")
                self.assertFalse(candidate["importable"])
                with self.assertRaises(Exception) as error:
                    service.handlers()["vms.import"]({
                        "id": "legacy-vm", "macos_version": "tahoe",
                        "expected_revision": f'"{service.store.revision}"',
                    })
            self.assertIn("não foi possível confirmar", str(error.exception).lower())

    def test_runtime_snapshot_distinguishes_stopped_from_unavailable(self):
        with patch("core.reims_ctl.reims_ctl_bin", return_value=Path("/usr/bin/reims-ctl")), patch(
            "core.reims_ctl.subprocess.run", return_value=type("Result", (), {
                "returncode": 0, "stdout": '[{"name":"legacy-vm","pid":null}]',
            })(),
        ):
            self.assertEqual(vm_runtime_snapshot(), {})
        with patch("core.reims_ctl.reims_ctl_bin", return_value=Path("/usr/bin/reims-ctl")), patch(
            "core.reims_ctl.subprocess.run", return_value=type("Result", (), {
                "returncode": 1, "stdout": "[]",
            })(),
        ):
            self.assertIsNone(vm_runtime_snapshot())

    def test_config_and_reims_registry_accept_explicit_absolute_namespace(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            catalog_dir = root / "state" / "vhackintosh"
            registry = root / "state" / "reims-ctl" / "vms.toml"
            env = os.environ.copy()
            env["VHACKINTOSH_CONFIG_DIR"] = str(catalog_dir)
            result = subprocess.run(
                [sys.executable, "-c", "from core.config import CONFIG_DIR; print(CONFIG_DIR)"],
                capture_output=True, text=True, check=True, env=env,
            )
            self.assertEqual(Path(result.stdout.strip()), catalog_dir)

            with patch.dict(os.environ, {"VHACKINTOSH_REIMS_REGISTRY": str(registry)}):
                self.assertEqual(registry_file(), registry)
            with patch.dict(os.environ, {"VHACKINTOSH_REIMS_REGISTRY": "relative/vms.toml"}):
                with self.assertRaisesRegex(ValueError, "caminho absoluto"):
                    registry_file()

    def test_agent_import_rejects_stale_catalog_revision(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            registry, _ = self.create_registered_vm(root)
            service = WebAgentService(root / "catalog.json", running_vms=lambda: {})
            old_revision = service.store.revision
            from core.config import VMConfig
            service.store.add_vm(VMConfig(id="other", name="Other", macos_version="tahoe"))
            with patch("core.reims_ctl.registry_file", return_value=registry), patch(
                "core.reims_ctl.vm_runtime_snapshot", return_value={},
            ):
                with self.assertRaises(Exception) as error:
                    service.handlers()["vms.import"]({
                        "id": "legacy-vm", "macos_version": "tahoe",
                        "expected_revision": f'"{old_revision}"',
                    })
            self.assertIn("mudou", str(error.exception).lower())


if __name__ == "__main__":
    unittest.main()
