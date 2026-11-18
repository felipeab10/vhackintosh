"""The vm.smbios.apply job (in-place config.plist edit) and the vms.smbios.generate preview."""

from __future__ import annotations

import plistlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.config import SMBIOSConfig, VMConfig
from core.graphics_profiles import EMBEDDED_KEXTS, GRAPHICS_PROFILES
from core.smbios import EDIT_MODEL_MAX_MACOS, GenSMBIOS, MACSERIAL_CANDIDATES
from core.web_agent import SMBIOS_APPLY_KIND, WebAgentService
from core.web_rpc import RpcRemoteError

LEGACY = "apple-gfx-pci"
# Pair measured with the vendored macserial (`-m iMac20,1 -g -n 1`), the model
# Dr. Strange's edit rules accept for tahoe VMs.
OLD = SMBIOSConfig(
    model="iMac20,1", serial_number="C02D38RCPN5T", board_serial="C02204300GUPHC1A8",
    smuuid="12345678-1234-5678-1234-567812345678", rom="02AABBCCDDEE",
)
# Pair measured with the vendored macserial (`-m iMac20,2 -g -n 1`); a model the
# real generator builds coherently (serial decodes to iMac20,2, MLB checksum valid).
NEW = SMBIOSConfig(
    model="iMac20,2", serial_number="C02CKMYF046M", board_serial="C02015500CD00001F",
    smuuid="23456789-1234-5678-1234-567812345678", rom="02AABBCCDDEE",
)
FIELDS = ("model", "serial_number", "board_serial", "smuuid", "rom")


def payload_identity(identity: SMBIOSConfig) -> dict[str, str]:
    return {field: getattr(identity, field) for field in FIELDS}


def expected_kexts():
    return set(EMBEDDED_KEXTS) | {bundle for bundle, _minimum in GRAPHICS_PROFILES[LEGACY]}


def config_plist(identity: SMBIOSConfig, picker: bool = True, boot_args: str = "-v custom=1",
                 sip: str = "AAAABA", patches: int = 2):
    """A config.plist with user customizations an in-place edit must preserve."""
    return {
        "Kernel": {
            "Add": [{"BundlePath": name, "Enabled": True} for name in sorted(expected_kexts())],
            "Patch": [{"Comment": f"hide-vm-{index}", "Enabled": True} for index in range(patches)],
        },
        "PlatformInfo": {"Generic": {
            "SystemProductName": identity.model,
            "SystemSerialNumber": identity.serial_number,
            "MLB": identity.board_serial,
            "SystemUUID": identity.smuuid,
            "ROM": bytes.fromhex(identity.rom),
        }},
        "Misc": {"Boot": {"ShowPicker": picker}, "Security": {"SecureBootModel": "Medium", "Vault": "Secure"}},
        "NVRAM": {"Add": {
            "7C436110-AB2A-4BBB-A880-FE41995C9F82": {"boot-args": "-v custom=1", "csr-active-config": sip},
            "prev-lang:kbd": "pt-BR:0",
        }},
    }


class Context:
    """Job context whose subprocesses emulate guestfish/qemu-img against a fake EFI."""

    def __init__(self, state: dict | None = None):
        if state is None:
            state = {"plist": config_plist(OLD)}
        self.checkpoints = []
        self.phases = []
        self.state = state

    def check_cancelled(self):
        return None

    def phase(self, name, progress=None):
        self.phases.append((name, progress))

    def checkpoint(self, name, payload=None, **_kwargs):
        self.checkpoints.append((name, payload))

    def run_subprocess(self, argv, check=True, timeout=None, **_kwargs):
        if argv[0] == "qemu-img":
            if self.state.get("qemu_img_fail"):
                return SimpleNamespace(returncode=1)
            return SimpleNamespace(returncode=0)
        if "download" in argv:
            Path(argv[-1]).write_bytes(plistlib.dumps(self.state["plist"]))
            return SimpleNamespace(returncode=0)
        if "upload" in argv:
            self.state["plist"] = plistlib.loads(Path(argv[-2]).read_bytes())
            self.state["mutated"] = True
            return SimpleNamespace(returncode=0)
        raise AssertionError(f"unexpected command: {argv}")


class Lock:
    def acquire(self, *_args, **_kwargs):
        return True

    def release(self):
        return None


class SmbiosApplyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = WebAgentService(Path(self.temp.name) / "catalog.json", running_vms=lambda: {})
        self.vm_dir = self.service.store.config_dir / "vms" / "studio"
        self.vm_dir.mkdir(parents=True)
        (self.vm_dir / "vm.toml").write_text('[q35]\nopencore = "OpenCore.qcow2"\n', encoding="utf-8")
        self.image = self.vm_dir / "OpenCore.qcow2"
        self.image.write_bytes(b"old EFI image bytes")
        self.service.store.add_vm(VMConfig(
            id="studio", name="Studio", macos_version="tahoe",
            disk_path=str(self.vm_dir / "macos.qcow2"), smbios=OLD,
        ))
        self.revision = self.service.store.revision

    def _install_fakes(self):
        """Patch only tool boundaries: guestfish/qemu-img (job context), macserial
        verification and the web_agent-side EFI reads. The plist injection itself
        (GenSMBIOS.inject_into_config_plist) runs for real, so the identity that
        lands in the fake EFI is the one the product code wrote."""
        state = {"plist": config_plist(OLD)}

        def verify(_smbios):
            return {"ok": True, "model_matches": True, "mlb_checksum": "valid",
                    "reason": "identity matches the decoded serial and the MLB checksum"}

        def webagent_run(command, **_kwargs):
            if "download" in command:
                Path(command[-1]).write_bytes(plistlib.dumps(state["plist"]))
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if "ls" in command:
                return SimpleNamespace(returncode=0, stdout="\n".join(sorted(expected_kexts())), stderr="")
            raise AssertionError(f"unexpected command: {command}")

        def stop(item):
            self.addCleanup(item.stop)
            item.start()

        stop(patch("core.reims_ctl.vm_record", return_value={
            "name": "studio", "dir": str(self.vm_dir), "state": "stopped", "pid": None,
        }))
        stop(patch("core.config.ExclusiveVMLock", Lock))
        stop(patch("core.web_agent.shutil.which", return_value="/usr/bin/guestfish"))
        stop(patch("core.smbios.GenSMBIOS.verify_with_macserial", side_effect=verify))
        stop(patch.object(WebAgentService, "_opencore_enabled_kexts", return_value=expected_kexts()))
        stop(patch("core.web_agent.subprocess.run", side_effect=webagent_run))
        self.state = state
        return state

    def _apply(self, context=None):
        return self.service._run_vm_smbios_apply_job({
            "vm_id": "studio", "smbios": payload_identity(NEW), "expected_revision": self.revision,
        }, context or Context(self.state))

    def test_apply_edits_plist_in_place_and_updates_catalog(self):
        state = self._install_fakes()
        context = Context(state)
        result = self._apply(context)
        saved = self.service.store.get_vm("studio")
        self.assertEqual(payload_identity(saved.smbios), payload_identity(NEW))
        self.assertEqual(result["changed_fields"], ["model", "serial_number", "board_serial", "smuuid"])
        self.assertTrue((self.vm_dir / ".opencore-before-smbios.qcow2").is_file())
        self.assertTrue((self.vm_dir / "smbios-backup-before-regeneration" / "OpenCore.qcow2").is_file())
        # No regeneration happened: kexts, SIP, kernel patches, Vault, SecureBootModel,
        # keyboard layout and boot-args all survive the identity edit.
        final = state["plist"]
        self.assertEqual([item["Comment"] for item in final["Kernel"]["Patch"]], ["hide-vm-0", "hide-vm-1"])
        self.assertEqual(final["NVRAM"]["Add"]["7C436110-AB2A-4BBB-A880-FE41995C9F82"]["csr-active-config"], "AAAABA")
        self.assertEqual(final["NVRAM"]["Add"]["prev-lang:kbd"], "pt-BR:0")
        self.assertEqual(final["Misc"]["Security"], {"SecureBootModel": "Medium", "Vault": "Secure"})
        self.assertEqual(final["NVRAM"]["Add"]["7C436110-AB2A-4BBB-A880-FE41995C9F82"]["boot-args"], "-v custom=1")
        self.assertTrue(final["Misc"]["Boot"]["ShowPicker"])
        self.assertEqual({item["BundlePath"] for item in final["Kernel"]["Add"]}, expected_kexts())
        # Audit checkpoint carries field names only, never identity values.
        self.assertEqual(context.checkpoints[0], ("smbios_apply_started", {
            "vm_id": "studio", "changed_fields": ["model", "serial_number", "board_serial", "smuuid"],
        }))
        self.assertNotIn(NEW.serial_number, str(context.checkpoints))
        self.assertNotIn(NEW.board_serial, str(context.checkpoints))

    def test_wrong_identity_in_edited_efi_fails_and_keeps_old_catalog(self):
        # Negative control: the EFI ends up carrying the OLD identity even though
        # the edit "succeeded". The job's own re-read of the five fields must
        # catch it, restore the image and never touch the catalog.
        original = self.image.read_bytes()
        state = self._install_fakes()

        def injector_with_wrong_identity(opencore_path, vm, show_picker=None, *, smbios=None, job_context=None):
            self.image.write_bytes(b"mutated by injector")
            state["plist"] = config_plist(OLD)

        with patch("core.disk.DiskProvisioner.inject_opencore_config", side_effect=injector_with_wrong_identity):
            with self.assertRaisesRegex(RuntimeError, "não carrega a identidade solicitada"):
                self._apply(Context(state))
        self.assertEqual(payload_identity(self.service.store.get_vm("studio").smbios), payload_identity(OLD))

    def test_failed_verification_restores_the_image_byte_for_byte(self):
        original = self.image.read_bytes()
        state = self._install_fakes()

        def injector_with_wrong_identity(opencore_path, vm, show_picker=None, *, smbios=None, job_context=None):
            self.image.write_bytes(b"mutated by injector")
            state["plist"] = config_plist(OLD)

        with patch("core.disk.DiskProvisioner.inject_opencore_config", side_effect=injector_with_wrong_identity):
            with self.assertRaises(RuntimeError):
                self._apply(Context(state))
        self.assertEqual(self.image.read_bytes(), original)

    def test_macserial_refusal_fails_closed_before_touching_the_image(self):
        original = self.image.read_bytes()
        state = self._install_fakes()

        def refuse(_smbios):
            return {"ok": False, "model_matches": False, "mlb_checksum": "invalid",
                    "reason": f"serial decodes to 'iMac20,1', not 'iMac20,2'"}

        with patch("core.smbios.GenSMBIOS.verify_with_macserial", side_effect=refuse):
            with self.assertRaises(RuntimeError) as caught:
                self._apply(Context(state))
        self.assertNotIn("iMac20,2", str(caught.exception))
        self.assertNotIn(NEW.serial_number, str(caught.exception))
        self.assertEqual(self.image.read_bytes(), original)
        self.assertEqual(payload_identity(self.service.store.get_vm("studio").smbios), payload_identity(OLD))

    def test_patch_vm_config_refuses_catalog_only_identity_change(self):
        with self.assertRaises(RpcRemoteError) as caught:
            self.service.patch_vm_config({
                "vm_id": "studio", "fields": {"smbios": payload_identity(NEW)},
                "expected_revision": self.revision,
            })
        self.assertEqual(caught.exception.code, "smbios_patch_forbidden")
        self.assertEqual(payload_identity(self.service.store.get_vm("studio").smbios), payload_identity(OLD))

    def test_preview_generates_five_fields_without_persisting(self):
        before = self.service.store.revision
        with patch("core.smbios.GenSMBIOS.generate_for_model", return_value=NEW), \
             patch("core.smbios.GenSMBIOS.verify_with_macserial", return_value={"ok": True}):
            result = self.service.generate_vm_smbios({"vm_id": "studio", "model": "iMac20,2"})
        self.assertEqual(result, payload_identity(NEW))
        self.assertEqual(self.service.store.revision, before)

    def test_preview_rejects_an_invalid_model_without_persisting(self):
        before = self.service.store.revision
        with self.assertRaises(RpcRemoteError) as caught:
            self.service.generate_vm_smbios({"vm_id": "studio", "model": "nao/um-modelo"})
        self.assertEqual(caught.exception.code, "invalid_smbios_model")
        self.assertEqual(self.service.store.revision, before)

    def test_preview_rejects_an_unknown_vm(self):
        with self.assertRaises(RpcRemoteError) as caught:
            self.service.generate_vm_smbios({"vm_id": "ghost", "model": "iMac20,2"})
        self.assertEqual(caught.exception.code, "vm_not_found")

    def test_submit_queues_versioned_job_for_the_vm(self):
        class Runner:
            def submit(self, **kwargs):
                self.kwargs = kwargs
                return {"id": "smbios-job"}, True

        runner = Runner()
        self.service.job_runner = runner
        with patch("core.smbios.GenSMBIOS.verify_with_macserial", return_value={"ok": True}):
            accepted = self.service.submit_vm_smbios_apply({
                "vm_id": "studio", "confirmation": "studio",
                "idempotency_key": "00000000-0000-4000-8000-000000000004",
                "expected_revision": self.revision, "smbios": payload_identity(NEW),
            })
        self.assertEqual(accepted["job_id"], "smbios-job")
        self.assertEqual(runner.kwargs["kind"], SMBIOS_APPLY_KIND)
        self.assertEqual(runner.kwargs["payload"]["smbios"]["model"], "iMac20,2")
        self.assertEqual(runner.kwargs["target_vm_id"], "studio")


class RealMacserialTests(unittest.TestCase):
    """Measure the pinned binary: the whole edit allowlist must generate a
    coherent identity (this is what the preview offers to the user)."""

    MACSERIAL = next(
        (candidate for candidate in MACSERIAL_CANDIDATES if candidate.is_file()),
        None,
    )

    @unittest.skipUnless(MACSERIAL is not None, "vendored macserial not present")
    def test_every_allowlisted_model_generates_an_identity_macserial_confirms(self):
        for model in sorted(EDIT_MODEL_MAX_MACOS):
            with self.subTest(model=model):
                # Use the newest macOS the model itself supports: some allowlisted
                # models (e.g. iMac18,3) top out below sequoia.
                macos_version = EDIT_MODEL_MAX_MACOS[model]
                identity = GenSMBIOS.generate_for_model(model, macos_version=macos_version)
                verification = GenSMBIOS.verify_with_macserial(identity)
                self.assertEqual(verification.get("model_matches"), True, verification)
                self.assertEqual(verification.get("mlb_checksum"), "valid", verification)

if __name__ == "__main__":
    unittest.main()
