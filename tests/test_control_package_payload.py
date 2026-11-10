from __future__ import annotations

import configparser
import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ControlPackagePayloadTests(unittest.TestCase):
    def test_package_builds_and_stages_the_pinned_apv_reims_util(self):
        builder = (ROOT / "packaging/common/build-reims-util.sh").read_text(encoding="utf-8")
        pin = (ROOT / "packaging/reims-util.pin").read_text(encoding="utf-8")
        build_all = (ROOT / "packaging/build-all.sh").read_text(encoding="utf-8")
        workflow = (ROOT / ".github/workflows/release-control.yml").read_text(encoding="utf-8")
        revision = "0b9c3780a3ec2aa312a448b09b18320e5a2c9251"

        # O SHA vive no pin único, lido pelo builder — não hardcoded nele.
        self.assertIn(f"REV={revision}", pin)
        self.assertIn("reims-util-pin.sh", builder)
        self.assertNotIn(revision, builder)
        self.assertIn("cargo \"+${TOOLCHAIN}\" build --locked --release", builder)
        self.assertIn("--add-kext", builder)
        self.assertNotIn("--no-apv-arm64-port", builder)
        self.assertIn('"${PAYLOAD_BASE}/opt/vhackintosh/resources/reims-util"', build_all)
        self.assertIn("/opt/vhackintosh/resources/reims-util/reims-util", workflow)
        self.assertIn("--add-kext", workflow)
        self.assertNotIn("--no-apv-arm64-port", workflow)

    def test_stage_app_copies_all_vendored_kexts_into_the_control_bundle(self):
        stage = (ROOT / "packaging/common/stage-app.sh").read_text(encoding="utf-8")
        source = ROOT / "resources/reims-kexts"
        staged_source = "${ROOT}/resources/reims-kexts/"
        expected = (
            "ReimsVgpu.kext",
            "AppleGraphicsDeviceControlBridge.kext",
            "AppleParavirtGPULoader.kext",
            "AppleParavirtIOSurface.kext",
            "IOGPUFamilyBridge.kext",
            "AppleParavirtGPUIOGPUFamily.kext",
            "IOGraphicsFamilyBridge.kext",
            "APVDisplayPort.kext",
        )
        self.assertIn(f'rsync -a --delete "{staged_source}" "${{APP}}/resources/reims-kexts/"', stage)
        for name in expected:
            with self.subTest(kext=name):
                self.assertTrue((source / name / "Contents/Info.plist").is_file())
                self.assertTrue((source / name).is_dir(), name)
        for metadata in ("LICENSE.txt", "PROVENANCE.md", "upstream-sources.sha256"):
            self.assertTrue((source / metadata).is_file(), metadata)

        with tempfile.TemporaryDirectory() as temporary:
            destdir = Path(temporary)
            staged = destdir / "opt/vhackintosh/web/bundles/.control-staging/resources/reims-kexts"
            staged.mkdir(parents=True)
            subprocess.run(
                ["rsync", "-a", "--delete", f"{source}/", f"{staged}/"], check=True
            )
            names = sorted(path.name for path in staged.glob("*.kext"))
            self.assertEqual(names, sorted(expected))
            self.assertEqual(len(names), 8)
            for path in (staged, *(staged / name for name in expected)):
                self.assertEqual(path.stat().st_mode & 0o777, 0o755)
            # Os kexts vendorizados são dados: os executáveis Mach-O e os
            # `Info.plist` de cada `Contents/` são 0755, os metadados soltos 0644.
            # Varremos apenas os oito kexts do conjunto legado: `resources/reims-kexts/`
            # pode conter árvores de perfil irmãs (apple-gfx-pci/, reims-graphics-pci/)
            # cujo layout/modo não é assunto deste contrato.
            scanned = [staged / name for name in expected]
            for path in [p for root_dir in scanned for p in root_dir.rglob("*")]:
                if not path.is_file():
                    continue
                inside_contents = path.parent.name == "MacOS" or path.parent.name == "Contents"
                expected_mode = 0o755 if inside_contents else 0o644
                self.assertEqual(path.stat().st_mode & 0o777, expected_mode, str(path))
            for metadata in ("LICENSE.txt", "PROVENANCE.md", "upstream-sources.sha256"):
                self.assertTrue((staged / metadata).is_file(), metadata)

    def test_staging_includes_the_pinned_macserial_tool_and_redirect_service(self):
        stage = (ROOT / "packaging/common/stage-app.sh").read_text(encoding="utf-8")
        binary = ROOT / "resources/macserial/macserial"
        expected_sha256 = "b0a4a3581c0f06b3ddce915165eb3a1726acfa876192fe9ee5ef9ea08a469ad8"

        self.assertEqual(hashlib.sha256(binary.read_bytes()).hexdigest(), expected_sha256)
        self.assertIn('"${ROOT}/resources/macserial/macserial"', stage)
        self.assertIn('"${ROOT}/resources/macserial/LICENSE.txt"', stage)
        self.assertIn('"${ROOT}/resources/macserial/README.md"', stage)
        self.assertIn(expected_sha256, stage)
        self.assertIn('"${ROOT}/packaging/systemd/vhackintosh-http-redirect.service"', stage)
        self.assertIn('"${DESTDIR}/usr/lib/systemd/system/vhackintosh-http-redirect.service"', stage)

    def test_staged_control_bundle_normalizes_restrictive_source_permissions(self):
        stage = (ROOT / "packaging/common/stage-app.sh").read_text(encoding="utf-8")
        normalizer = ROOT / "packaging/common/normalize-tree-permissions.sh"
        self.assertIn('normalize-tree-permissions.sh" "${APP}"', stage)

        with tempfile.TemporaryDirectory() as temporary:
            bundle = Path(temporary) / "bundle"
            core = bundle / "core"
            core.mkdir(parents=True)
            unreadable_module = core / "hardware.py"
            unreadable_module.write_text("value = 1\n", encoding="utf-8")
            unreadable_module.chmod(0o600)
            executable = bundle / "launcher"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o700)
            core.chmod(0o700)

            subprocess.run(["bash", str(normalizer), str(bundle)], check=True)

            self.assertEqual(os.stat(core).st_mode & 0o777, 0o755)
            self.assertEqual(os.stat(unreadable_module).st_mode & 0o777, 0o644)
            self.assertEqual(os.stat(executable).st_mode & 0o777, 0o755)

    def test_desktop_package_stages_the_matching_ovmf_code_and_vars_templates(self):
        stage = (ROOT / "packaging/common/stage-app.sh").read_text(encoding="utf-8")
        self.assertIn('"${APP}/templates"', stage)
        self.assertIn('"${ROOT}/templates/OVMF_VARS.fd"', stage)
        self.assertIn('"${ROOT}/templates/OVMF_CODE_4M.fd"', stage)

    def test_all_package_managers_enable_the_redirect_unit(self):
        scripts = {
            "Arch": ROOT / "packaging/arch/vhackintosh-control.install",
            "Debian": ROOT / "packaging/deb/postinst",
            "Fedora": ROOT / "packaging/fedora/vhackintosh-control.spec",
        }
        for distro, path in scripts.items():
            with self.subTest(distro=distro):
                self.assertIn("vhackintosh-http-redirect.service", path.read_text(encoding="utf-8"))

    def test_redirect_unit_is_hardened_and_can_bind_port_80_as_service_user(self):
        unit = ROOT / "packaging/systemd/vhackintosh-http-redirect.service"
        text = unit.read_text(encoding="utf-8")
        self.assertIn("User=vhackintosh-web", text)
        self.assertIn("Environment=VHACKINTOSH_HTTP_PORT=80", text)
        self.assertIn("Environment=VHACKINTOSH_WEB_PORT=8443", text)
        self.assertIn("AmbientCapabilities=CAP_NET_BIND_SERVICE", text)
        self.assertIn("CapabilityBoundingSet=CAP_NET_BIND_SERVICE", text)
        self.assertIn("NoNewPrivileges=yes", text)
        self.assertRegex(text, r"(?m)^ExecStart=/usr/bin/python3 -m web\.api\.http_redirect$")

    def test_fedora_opt_files_entries_are_created_by_package_staging(self):
        spec = (ROOT / "packaging/fedora/vhackintosh-control.spec").read_text(encoding="utf-8")
        stage = (ROOT / "packaging/common/stage-app.sh").read_text(encoding="utf-8")
        build_all = (ROOT / "packaging/build-all.sh").read_text(encoding="utf-8")
        files_section = spec.split("%files\n", 1)[1].split("%changelog", 1)[0]
        entries = [
            line.strip() for line in files_section.splitlines()
            if line.strip().startswith(("/opt/", "/usr/", "/etc/"))
        ]
        self.assertTrue(entries)

        # The RPM owns `/opt/vhackintosh/web/bundles` recursively, so an
        # accidental separate `/opt/vhackintosh/resources/reims-kexts` entry is
        # specifically invalid: only macserial/reims-util live at resources/.
        self.assertIn("/opt/vhackintosh/web/bundles", entries)
        self.assertNotIn("/opt/vhackintosh/resources/reims-kexts", entries)
        for entry in entries:
            with self.subTest(entry=entry):
                if entry == "/opt/vhackintosh/web/current":
                    self.assertIn('ln -s "bundles/.control-staging" "${WEB_ROOT}/current"', stage)
                elif entry == "/opt/vhackintosh/web/bundles":
                    self.assertIn('install -d "${WEB_ROOT}/bundles"', stage)
                elif entry.startswith("/opt/vhackintosh/resources/"):
                    resource = entry.removeprefix("/opt/vhackintosh/resources/")
                    self.assertTrue(
                        f'"${{ROOT}}/resources/{resource}' in stage
                        or f'"${{PAYLOAD_BASE}}/opt/vhackintosh/resources/{resource}' in build_all,
                        f"%files lists {entry}, but no staging script creates it",
                    )

    def test_fedora_package_owns_the_shared_tool_and_redirect_unit(self):
        spec = (ROOT / "packaging/fedora/vhackintosh-control.spec").read_text(encoding="utf-8")
        self.assertRegex(spec, r"(?m)^/opt/vhackintosh/resources/macserial$")
        self.assertRegex(spec, r"(?m)^/usr/lib/systemd/system/vhackintosh-http-redirect\.service$")

    def test_release_workflow_kext_guard_checks_the_runtime_table_not_a_stale_grep(self):
        """O guarda de kexts do release não pode ser um `grep` textual morto.

        A frente B moveu a resolução de kexts de `core/reims_util.py` para
        `core/graphics_profiles.py`. O guarda antigo
        (`grep -q 'reims-kexts' .../core/reims_util.py`) passou a dar 0 matches
        com um payload SAUDÁVEL e derrubou o Release beta.23. O novo guarda importa
        a tabela no runtime instalado e confere que os kexts estão ao lado do
        bundle — um contrato de runtime, não uma substring.
        """
        workflow_text = (ROOT / ".github/workflows/release-control.yml").read_text(
            encoding="utf-8"
        )
        # O defeito raiz: não voltar a grepar o arquivo errado.
        self.assertNotIn("grep -q 'reims-kexts'", workflow_text)
        self.assertIn("import core.graphics_profiles", workflow_text)

        guard = [
            line.strip()
            for line in workflow_text.splitlines()
            if line.strip().startswith("python3 -c '") and "core.graphics_profiles" in line
        ]
        self.assertEqual(len(guard), 1, "esperado exatamente um guarda de runtime")
        guard_cmd = guard[0]

        def run_guard(bundle: Path) -> subprocess.CompletedProcess[str]:
            program = f'bundle_root="{bundle}"; {guard_cmd}'
            return subprocess.run(
                ["bash", "-c", program],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60,
                cwd=str(ROOT),
            )

        profile_module = (ROOT / "core/graphics_profiles.py").read_text(encoding="utf-8")
        expected = (
            "ReimsVgpu.kext",
            "AppleGraphicsDeviceControlBridge.kext",
            "AppleParavirtGPULoader.kext",
            "AppleParavirtIOSurface.kext",
            "IOGPUFamilyBridge.kext",
            "AppleParavirtGPUIOGPUFamily.kext",
            "IOGraphicsFamilyBridge.kext",
            "APVDisplayPort.kext",
        )
        with tempfile.TemporaryDirectory() as temporary:
            bundle = Path(temporary) / "bundle"
            (bundle / "core").mkdir(parents=True)
            (bundle / "core/graphics_profiles.py").write_text(profile_module, encoding="utf-8")
            kext_root = bundle / "resources/reims-kexts"
            for name in expected:
                (kext_root / name).mkdir(parents=True)

            ok = run_guard(bundle)
            self.assertEqual(ok.returncode, 0, ok.stderr)

            # Defeito real: o bundle perde os kexts vendorizados. O guarda antigo
            # (grep textual) passaria aqui onde não há kext nenhum; este não.
            (kext_root / expected[0]).rmdir()
            failed = run_guard(bundle)
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn(expected[0], failed.stderr + failed.stdout)

    def test_desktop_vm_unit_uses_the_logged_in_display_instead_of_seizing_a_vt(self):
        unit_path = ROOT / "packaging/systemd/vhackintosh-vm@.service"
        unit_text = unit_path.read_text(encoding="utf-8")
        unit = configparser.ConfigParser(strict=False, interpolation=None)
        unit.read_string(unit_text)

        self.assertIn("Environment=VHACKINTOSH_VM_DISPLAY_MODE=desktop", unit_text)
        self.assertNotIn("vhackintosh-display.service", unit["Unit"].get("Requires", ""))
        self.assertNotIn("vhackintosh-display.service", unit["Unit"].get("After", ""))
        self.assertNotIn("Environment=XDG_RUNTIME_DIR", unit_text)
        self.assertNotIn("Environment=WAYLAND_DISPLAY", unit_text)

        stage = (ROOT / "packaging/common/stage-app.sh").read_text(encoding="utf-8")
        self.assertIn('"${ROOT}/packaging/systemd/vhackintosh-vm@.service"', stage)


if __name__ == "__main__":
    unittest.main()
