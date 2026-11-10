"""Frente D: graphics profile of the selected QEMU and exact EFI kext sets.

The closed contract says the graphics profile is identified by the compiled
device name (`reims-graphics-pci` wins over `apple-gfx-pci`; absence of both is
fail-closed) and that an EFI must enable exactly `{Lilu.kext} ∪ profile kexts`.
These tests are hermetic: no real QEMU, reims-util or guestfish is executed.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.config import SMBIOSConfig, VMConfig, VMManagerStore
from core.graphics_profiles import EMBEDDED_KEXTS, GRAPHICS_PROFILES
from core.runtime_inventory import _qemu_graphics_profile, inspect_runtime
from core.web_agent import WebAgentService

LEGACY = "apple-gfx-pci"
REIMS = "reims-graphics-pci"


def _bundle_names(profile: str) -> set[str]:
    return {bundle for bundle, _minimum in GRAPHICS_PROFILES[profile]}


def _write_binary(root: Path, name: str, blob: bytes) -> Path:
    path = root / name
    path.write_bytes(blob)
    path.chmod(0o700)
    return path


def _qemu_script(root: Path, name: str = "qemu", devices: bytes = b"") -> Path:
    path = root / name
    path.write_text(
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        "  --version) echo \"QEMU emulator version 11.1\" ;;\n"
        "  -help) printf '%s\\n' '-rtc [base=utc]' '-msg [timestamp=on]' '-device driver' ;;\n"
        "  -device) printf '%s\\n' 'name \"virtio-blk-pci\", bus PCI' ;;\n"
        "  -machine) printf '%s\\n' 'Supported machines are:' 'q35  Q35 machine' ;;\n"
        "  -cpu) printf '%s\\n' 'Available CPUs:' 'host  host processor' ;;\n"
        "  -audiodev) printf '%s\\n' 'Available audio drivers:' 'none' ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    # Device names are compiled strings, not printed output: append them as data
    # bytes so the byte-level probe sees exactly what a real binary would carry.
    with path.open("ab") as stream:
        stream.write(b"\x00" + devices)
    path.chmod(0o700)
    return path


class QemuGraphicsProfileProbeTests(unittest.TestCase):
    def test_both_devices_compiled_is_a_development_build_and_new_wins(self):
        """Both devices is the development build, not an ambiguity.

        The development QEMU compiles `apple-gfx-pci` for compatibility plus
        `reims-graphics-pci`, so the new device must win. This must agree with
        `core.reims_release._device_of_this_binary` (measured: `new+apple ->
        "graphics"`); asserting both sides keeps them from drifting apart.
        """
        with tempfile.TemporaryDirectory() as temp:
            binary = _write_binary(Path(temp), "qemu", b"...reims-graphics-pci...apple-gfx-pci...")
            self.assertEqual(_qemu_graphics_profile(binary), REIMS)
            from core.reims_release import _device_of_this_binary
            self.assertEqual(_device_of_this_binary(binary), "graphics")

    def test_legacy_is_selected_only_when_the_new_device_is_absent(self):
        with tempfile.TemporaryDirectory() as temp:
            binary = _write_binary(Path(temp), "qemu", b"...apple-gfx-pci...")
            self.assertEqual(_qemu_graphics_profile(binary), LEGACY)

    def test_absent_or_unreadable_binary_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertIsNone(_qemu_graphics_profile(None))
            self.assertIsNone(_qemu_graphics_profile(root / "does-not-exist"))
            unknown = _write_binary(root, "unknown", b"...virtio-gpu...")
            self.assertIsNone(_qemu_graphics_profile(unknown))

    def test_permission_denied_is_not_treated_as_a_profile(self):
        if os.geteuid() == 0:
            self.skipTest("running as root: chmod 000 still readable")
        with tempfile.TemporaryDirectory() as temp:
            binary = _write_binary(Path(temp), "qemu", b"...reims-graphics-pci...")
            binary.chmod(0o000)
            try:
                self.assertIsNone(_qemu_graphics_profile(binary))
            finally:
                binary.chmod(0o700)


class InventoryGraphicsProfileTests(unittest.TestCase):
    def test_inventory_reports_the_profile_of_the_selected_qemu(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            qemu = _qemu_script(root, devices=b">reims-graphics-pci<")
            reims = root / "reims-ctl"
            reims.write_text("#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then echo 'reims-ctl 8.1'; fi\n", encoding="utf-8")
            reims.chmod(0o700)
            with patch("core.runtime_inventory._cache", None), \
                 patch("core.reims_util.find_reims_util", return_value=None), \
                 patch.dict("os.environ", {"REIMS_CTL_UTIL": ""}, clear=False):
                result = inspect_runtime(reims_binary=reims, qemu_binary=qemu, use_cache=False)
        self.assertEqual(result["qemu_graphics_profile"], REIMS)
        self.assertEqual(result["qemu_graphics_profile_source"], str(qemu))

    def test_inventory_leaves_profile_unset_when_the_binary_is_ambiguous(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            qemu = _qemu_script(root, devices=b">virtio-gpu<")
            reims = root / "reims-ctl"
            reims.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            reims.chmod(0o700)
            with patch("core.runtime_inventory._cache", None), \
                 patch("core.reims_util.find_reims_util", return_value=None), \
                 patch.dict("os.environ", {"REIMS_CTL_UTIL": ""}, clear=False):
                result = inspect_runtime(reims_binary=reims, qemu_binary=qemu, use_cache=False)
        self.assertIsNone(result["qemu_graphics_profile"])
        self.assertIsNone(result["qemu_graphics_profile_source"])

    def test_inventory_honors_an_explicit_ctl_util_override(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            qemu = _qemu_script(root, devices=b">apple-gfx-pci<")
            reims = root / "reims-ctl"
            reims.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            reims.chmod(0o700)
            util = root / "reims-util"
            util.write_text(
                "#!/bin/sh\nif [ \"$1 $2\" = \"opencore --help\" ]; then echo '--model <MODEL>'; fi\n",
                encoding="utf-8",
            )
            util.chmod(0o700)
            with patch("core.runtime_inventory._cache", None), \
                 patch("core.reims_util.find_reims_util") as resolver, \
                 patch.dict("os.environ", {"REIMS_CTL_UTIL": str(util)}, clear=False):
                result = inspect_runtime(reims_binary=reims, qemu_binary=qemu, use_cache=False)
        resolver.assert_not_called()
        self.assertEqual(result["qemu_graphics_profile"], LEGACY)
        self.assertEqual(result["reims_update_opencore_kext_mode_source"], str(util))


class UtilitySelectionConsistencyTests(unittest.TestCase):
    """Generator, inventory and both ctl call sites must use one util binary.

    Two divergent fake binaries stand in for a stale companion versus the
    package-first pick; only the resolver's answer may reach the child env.
    """

    def _fake_util(self, root: Path, name: str, mode: str) -> Path:
        path = root / name
        path.write_text(
            "#!/bin/sh\n"
            "case \"$1 $2\" in\n"
            f"  \"--version \") echo \"reims-util 0.1.0 ({mode})\" ;;\n"
            "  \"opencore --help\") echo '--model <MODEL> --no-apv-arm64-port' ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        path.chmod(0o700)
        return path

    def test_inventory_and_ctl_environment_agree_on_the_resolved_util(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ctl = root / "bin" / "reims-ctl"
            ctl.parent.mkdir()
            ctl.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            ctl.chmod(0o700)
            selected = self._fake_util(root, "package-reims-util", "package-first")
            stale = root / "companion-reims-util"
            stale.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            stale.chmod(0o700)
            # The companion beside reims-ctl is deliberately the OTHER binary.
            companion = ctl.parent / "reims-util"
            companion.write_bytes(stale.read_bytes())
            companion.chmod(0o700)

            with patch("core.reims_util.find_reims_util", return_value=selected), \
                 patch("core.runtime_inventory._cache", None), \
                 patch.dict("os.environ", {"REIMS_CTL_UTIL": ""}, clear=False):
                from core.runtime_inventory import inspect_runtime
                runtime = inspect_runtime(reims_binary=ctl, qemu_binary=None, use_cache=False)

        self.assertEqual(runtime["reims_update_opencore_kext_mode_source"], str(selected))
        self.assertEqual(runtime["reims_update_opencore_kext_mode"], "embedded")
        self.assertNotEqual(runtime["reims_update_opencore_kext_mode_source"], str(companion))


class ExactKextSetValidationTests(unittest.TestCase):
    """The EFI profile gate must accept only the exact declared kext set."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = WebAgentService(
            Path(self.temp.name) / "vms.json",
            running_vms=lambda: {},
        )

    def test_expected_sets_match_the_closed_contract(self):
        expected_legacy = self.service._expected_opencore_kexts(LEGACY)
        expected_reims = self.service._expected_opencore_kexts(REIMS)
        self.assertEqual(expected_legacy, set(EMBEDDED_KEXTS) | {b for b, _ in GRAPHICS_PROFILES[LEGACY]})
        self.assertEqual(expected_reims, set(EMBEDDED_KEXTS) | {b for b, _ in GRAPHICS_PROFILES[REIMS]})
        self.assertEqual(len(expected_legacy), 9)
        self.assertEqual(len(expected_reims), 6)
        self.assertNotEqual(expected_legacy, expected_reims)

    def test_unknown_profile_is_refused(self):
        with self.assertRaises(RuntimeError):
            self.service._expected_opencore_kexts("not-a-profile")

    def test_discriminator_recognizes_each_profile(self):
        self.assertEqual(self.service._graphics_profile_from_kexts(_bundle_names(LEGACY) | set(EMBEDDED_KEXTS)), LEGACY)
        self.assertEqual(self.service._graphics_profile_from_kexts(_bundle_names(REIMS) | set(EMBEDDED_KEXTS)), REIMS)

    def test_discriminator_fails_closed_on_absent_or_ambiguous_markers(self):
        both = _bundle_names(LEGACY) | _bundle_names(REIMS) | set(EMBEDDED_KEXTS)
        self.assertIsNone(self.service._graphics_profile_from_kexts(both))
        self.assertIsNone(self.service._graphics_profile_from_kexts(set(EMBEDDED_KEXTS)))
        self.assertIsNone(self.service._graphics_profile_from_kexts(set()))

    def test_discriminator_rejects_a_same_count_cross_profile_swap(self):
        # The reims profile has RgfxGate but no legacy marker; the legacy set
        # with RgfxGate removed must not be read as reims.
        legacy_without_gate = _bundle_names(LEGACY) | set(EMBEDDED_KEXTS)
        legacy_without_gate.discard("RgfxGate.kext")
        self.assertEqual(self.service._graphics_profile_from_kexts(legacy_without_gate), LEGACY)


class PickerProfileMismatchTests(unittest.TestCase):
    """Picker must never let a profile change ride on a picker regeneration.

    `update-opencore` takes no profile argument, so an EFI built for one
    profile must be refused when the selected QEMU carries the other.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.service = WebAgentService(self.root / "vms.json", running_vms=lambda: {})

    def test_picker_refuses_when_efi_profile_differs_from_selected_qemu(self):
        vm = VMConfig(id="macos-studio", name="Studio", macos_version="tahoe", opencore_show_picker=False)
        self.service.store.add_vm(vm)
        vm_dir = self.service.store.config_dir / "vms" / vm.id
        vm_dir.mkdir(parents=True)
        (vm_dir / "vm.toml").write_text('[q35]\nopencore = "OpenCore.qcow2"\n', encoding="utf-8")
        image = vm_dir / "OpenCore.qcow2"
        image.write_bytes(b"EFI built for legacy, QEMU is reims")

        class Context:
            @staticmethod
            def check_cancelled(): return None
            @staticmethod
            def phase(*_a, **_k): return None
            @staticmethod
            def checkpoint(*_a, **_k): return None
            @staticmethod
            def log(*_a, **_k): return None

        class Lock:
            def acquire(self, *_a, **_k): return True
            def release(self): return None

        with patch("core.reims_ctl.vm_record", return_value={"name": vm.id, "dir": str(vm_dir), "state": "stopped", "pid": None}), \
             patch("core.reims_ctl.reims_ctl_bin", return_value="/managed/reims-ctl"), \
             patch("core.runtime_inventory.inspect_runtime", return_value=_runtime_dict(REIMS)), \
             patch.object(self.service, "_opencore_enabled_kexts", return_value=_bundle_names(LEGACY) | set(EMBEDDED_KEXTS)), \
             patch("core.config.ExclusiveVMLock", Lock), \
             patch("core.web_agent.shutil.which", return_value="/usr/bin/guestfish"), \
             patch("core.web_agent.subprocess.run") as run:
            # If the profile check is weakened to trusting the QEMU profile,
            # the exact-set check fires instead; only the dedicated mismatch
            # message proves the profile gate itself rejected the run.
            with self.assertRaisesRegex(RuntimeError, "picker não migra perfil"):
                self.service._run_vm_opencore_picker_job(
                    {"vm_id": vm.id, "boot_selection": True, "expected_revision": self.service.store.revision},
                    Context(),
                )
        run.assert_not_called()
        self.assertFalse(self.service.store.get_vm(vm.id).opencore_show_picker)


class ReleaseProfileMismatchTests(unittest.TestCase):
    """A VM built for another profile must be skipped, never rewritten.

    The release batch regenerates every managed EFI with the freshly installed
    QEMU. When a VM's EFI profile differs, `update-opencore` cannot migrate it,
    so the batch must skip it and leave the image byte-identical.
    """

    def test_release_skips_a_vm_whose_efi_profile_differs_from_the_installed_qemu(self):
        import plistlib
        from core.reims_release import UpdateResult

        with tempfile.TemporaryDirectory() as directory:
            service = WebAgentService(Path(directory) / "vms.json", gpu_inventory=lambda: [])
            vm = VMConfig(id="macos-studio", name="Studio", macos_version="tahoe")
            service.store.add_vm(vm)
            vm_dir = service.store.config_dir / "vms" / vm.id
            vm_dir.mkdir(parents=True)
            (vm_dir / "vm.toml").write_text('[q35]\nopencore = "OpenCore.qcow2"\n', encoding="utf-8")
            image = vm_dir / "OpenCore.qcow2"
            image.write_bytes(b"original image")
            fake_tree = Path(directory) / "release"
            fake_tree.mkdir()
            legacy_bundles = sorted(_bundle_names(LEGACY) | set(EMBEDDED_KEXTS))

            class Context:
                def __init__(self): self.events = []
                def check_cancelled(self): return None
                def phase(self, name, progress=None): self.events.append((name, progress))
                def checkpoint(self, name, payload, **_k): self.events.append((name, payload))
                def log(self, message, level=None): self.events.append(("log", message, level))
                def run_subprocess(self, *_a, **_k):
                    raise AssertionError("update-opencore must not run for a skipped VM")

            def fake_run(command, **_kwargs):
                if command[1:3] == ["update-opencore", "--help"]:
                    return SimpleNamespace(returncode=0, stdout="Usage: reims-ctl update-opencore <NAME>", stderr="")
                if command[0] == "guestfish" and "download" in command:
                    plist = {"Kernel": {"Add": [{"BundlePath": n, "Enabled": True} for n in legacy_bundles]}}
                    Path(command[-1]).write_bytes(plistlib.dumps(plist))
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                if command[0] == "guestfish" and "ls" in command:
                    return SimpleNamespace(returncode=0, stdout="\n".join(legacy_bundles) + "\n", stderr="")
                raise AssertionError(command)

            with patch("core.reims_release.update_from_release", return_value=UpdateResult(
                     ok=True, tag="v1.2.3", previous="v1.2.2", message="updated", changed=True,
                 )), patch("core.update_guard.maintenance_window"), patch(
                     "core.reims_build.prebuilt_reims_ctl", return_value=fake_tree / "bin/reims-ctl"
                 ), patch(
                     "core.reims_build.prebuilt_reims_util", return_value=fake_tree / "bin/reims-util"
                 ), patch("core.reims_build.install_prebuilt", return_value=True), patch(
                     "core.reims_ctl.reims_ctl_bin", return_value="/managed/reims-ctl"
                 ), patch(
                     "core.reims_ctl.vm_record", return_value={"name": vm.id, "dir": str(vm_dir), "state": "stopped", "pid": None}
                 ), patch("core.web_agent.shutil.which", return_value="/usr/bin/guestfish"), patch(
                     "core.web_agent.subprocess.run", side_effect=fake_run
                 ), patch(
                     "core.runtime_inventory.inspect_runtime", return_value=_runtime_dict(REIMS)
                 ):
                result = service._run_reims_release_update_job({"tag": "v1.2.3"}, Context())
                remaining = image.read_bytes()
            service.close_jobs()

        self.assertEqual(result["opencore_skipped"], [vm.id])
        self.assertEqual(result["opencore_updated"], [])
        self.assertEqual(remaining, b"original image")


def _runtime_dict(profile):
    return {
        "reims_update_opencore_picker": True,
        "reims_update_opencore_kext_mode": "embedded",
        "reims_update_opencore_kext_mode_source": "/managed/reims-util",
        "qemu_graphics_profile": profile,
        "qemu_graphics_profile_source": "/managed/qemu-system-x86_64",
    }


if __name__ == "__main__":
    unittest.main()
