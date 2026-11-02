"""Static ISO payload and service wiring checks for the web manager."""

from __future__ import annotations

import configparser
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from appliance.validate_iso import validate_web_manager_payload

ROOT = Path(__file__).resolve().parents[1]
AIROOTFS = ROOT / "appliance/archiso/airootfs"


class WebAppliancePackagingTests(unittest.TestCase):
    def test_vm_template_is_packaged_without_being_enabled(self):
        unit = AIROOTFS / "etc/systemd/system/vhackintosh-vm@.service"
        text = unit.read_text(encoding="utf-8")
        parser = configparser.ConfigParser(strict=False, interpolation=None)
        parser.read_string(text)
        service = parser["Service"]
        self.assertEqual(
            service["ExecStart"],
            "/usr/bin/python3 -m core.vm_supervisor --vm-id %i",
        )
        packages = (ROOT / "appliance/archiso/packages.x86_64").read_text(encoding="utf-8")
        self.assertRegex(packages, r"(?m)^util-linux$")
        self.assertEqual(service["KillMode"], "control-group")
        self.assertNotIn("Restart", service)
        self.assertIn(
            "Environment=VHACKINTOSH_VM_CATALOG=/root/.config/vhackintosh/vms.json",
            text,
        )
        self.assertIn("Environment=VHACKINTOSH_VM_ID=%i", text)
        self.assertIn("Environment=XDG_RUNTIME_DIR=/run/vhackintosh-display", text)
        self.assertIn("Environment=WAYLAND_DISPLAY=wayland-0", text)
        self.assertIn("vhackintosh-display.service", parser["Unit"].get("Requires", ""))
        self.assertEqual(service["User"], "root")
        self.assertFalse(
            (AIROOTFS / "etc/systemd/system/multi-user.target.wants/vhackintosh-vm@.service").exists(),
            "template must not be enabled automatically",
        )
        if shutil.which("systemd-analyze"):
            subprocess.run(
                ["systemd-analyze", "verify", str(unit)],
                check=True,
                capture_output=True,
                text=True,
            )

    def test_build_copies_vm_template_and_validator_requires_it(self):
        build = (ROOT / "appliance/build-iso.sh").read_text(encoding="utf-8")
        self.assertIn("vhackintosh-vm@.service", build)
        self.assertIn("install -m 0644", build)
        validator = (ROOT / "appliance/validate_iso.py").read_text(encoding="utf-8")
        self.assertIn('"etc/systemd/system/vhackintosh-vm@.service"', validator)
        self.assertIn("vhackintosh-display.service", build)
        self.assertIn("vhackintosh-wait-wayland", build)
        self.assertIn('"etc/systemd/system/vhackintosh-display.service"', validator)

    def test_shared_display_unit_owns_one_compositor_for_all_vm_windows(self):
        display_path = AIROOTFS / "etc/systemd/system/vhackintosh-display.service"
        display_text = display_path.read_text(encoding="utf-8")
        display = configparser.ConfigParser(strict=False, interpolation=None)
        display.read_string(display_text)
        service = display["Service"]
        self.assertEqual(service["TTYPath"], "/dev/tty2")
        self.assertEqual(service["StandardInput"], "tty-force")
        self.assertEqual(service["ExecStartPre"], "/usr/bin/chvt 2")
        self.assertNotIn("openvt --switch --wait", display_text)
        self.assertIn("vhackintosh-waysession", service["ExecStart"])
        self.assertIn("core.display_session_guard", service["ExecStart"])
        self.assertEqual(service["RuntimeDirectory"], "vhackintosh-display")
        self.assertIn("seatd.service", display["Unit"].get("Requires", ""))
        self.assertIn("seatd.service", display["Unit"].get("After", ""))
        self.assertIn("WLR_BACKENDS=drm,libinput", service["Environment"])
        self.assertNotIn("Environment=WAYLAND_DISPLAY=wayland-0", display_text)
        self.assertIn("ExecStartPost=/usr/bin/bash /usr/local/bin/vhackintosh-wait-wayland", display_text)
        self.assertEqual(service["KillMode"], "control-group")
        if shutil.which("systemd-analyze"):
            subprocess.run(
                ["systemd-analyze", "verify", str(display_path)],
                check=True,
                capture_output=True,
                text=True,
            )

    def test_seatd_is_installed_and_enabled_for_live_and_installed_appliances(self):
        packages = (ROOT / "appliance/archiso/packages.x86_64").read_text(encoding="utf-8")
        self.assertRegex(packages, r"(?m)^seatd$")
        wants = AIROOTFS / "etc/systemd/system/multi-user.target.wants/seatd.service"
        self.assertTrue(wants.is_symlink())
        self.assertTrue((AIROOTFS / "usr/local/bin/vhackintosh-install").exists())
        installer = (AIROOTFS / "usr/local/bin/vhackintosh-install").read_text(encoding="utf-8")
        self.assertIn("/mnt/usr/lib/systemd/system/seatd.service", installer)

    def test_build_downloads_and_marks_reims_before_packaging_web_bundle(self):
        build = (ROOT / "appliance/build-iso.sh").read_text(encoding="utf-8")
        release_marker = build.index('> "${REIMS_DEST}/.release"')
        provenance = build.index('> "${REIMS_DEST}/SOURCE-PROVENANCE.txt"')
        web_bundle = build.index('bash "${SCRIPT_DIR}/lib/package_web_manager.sh"')
        self.assertLess(release_marker, provenance)
        self.assertLess(
            provenance,
            web_bundle,
            "the web bundle needs the tagged Reims release in the staged ISO rootfs",
        )

    def test_large_reims_release_download_defaults_to_persistent_temp_storage(self):
        build = (ROOT / "appliance/build-iso.sh").read_text(encoding="utf-8")
        temp_dir = build.index('REIMS_TMP_DIR="${TMPDIR:-/var/tmp}"')
        temp_guard = build.index('[ ! -d "${REIMS_TMP_DIR}" ]', temp_dir)
        temp_file = build.index(
            'mktemp "${REIMS_TMP_DIR%/}/reims-vgpu-release-XXXXXX.tar.gz"',
            temp_guard,
        )
        download = build.index('curl -fL --progress-bar -o "${REIMS_TMP_TAR}"', temp_file)
        self.assertLess(temp_dir, temp_guard)
        self.assertLess(temp_guard, temp_file)
        self.assertLess(
            temp_file,
            download,
            "the release archive must be created in the selected writable temp directory",
        )

    def test_systemd_services_have_expected_runtime_and_security_boundary(self):
        agent_path = AIROOTFS / "etc/systemd/system/vhackintosh-agent.service"
        web_path = AIROOTFS / "etc/systemd/system/vhackintosh-web.service"
        for path, module in ((agent_path, "core.web_agent"), (web_path, "web.api.server")):
            with self.subTest(unit=path.name):
                parser = configparser.ConfigParser(strict=False)
                parser.read_string(path.read_text(encoding="utf-8"))
                self.assertEqual(parser["Service"]["Type"], "simple")
                self.assertEqual(parser["Service"]["WorkingDirectory"], "/opt/vhackintosh/web/current")
                self.assertEqual(parser["Service"]["ExecStart"], f"/usr/bin/python3 -m {module}")
                self.assertEqual(parser["Service"]["Restart"], "on-failure")
                self.assertEqual(parser["Service"]["UMask"], "0077")
                self.assertEqual(parser["Service"]["NoNewPrivileges"], "yes")
                if path == agent_path:
                    self.assertEqual(parser["Service"]["KillMode"], "control-group")

        agent = agent_path.read_text(encoding="utf-8")
        self.assertIn("User=root", agent)
        self.assertIn("VHACKINTOSH_VM_CATALOG=/root/.config/vhackintosh/vms.json", agent)
        self.assertIn("VHACKINTOSH_CONFIG_DIR=/root/.config/vhackintosh", agent)
        self.assertIn("VHACKINTOSH_REIMS_REGISTRY=/root/.config/reims-ctl/vms.toml", agent)
        self.assertIn("XDG_CONFIG_HOME=/root/.config", agent)
        self.assertIn("XDG_DATA_HOME=/root/.local/share", agent)
        self.assertIn("CARGO_HOME=/root/.cargo", agent)
        self.assertIn("Group=vhackintosh-web", agent)
        self.assertIn("VHACKINTOSH_WEB_GROUP=vhackintosh-web", agent)
        web = web_path.read_text(encoding="utf-8")
        self.assertIn("After=network.target", web)
        self.assertNotIn("network-online.target", web)
        self.assertNotIn("Wants=network-online.target", web)
        self.assertIn("VHACKINTOSH_WEB_FRONTEND_DIST=/opt/vhackintosh/web/current/web/frontend/dist", web)
        self.assertIn("User=vhackintosh-web", web)
        self.assertIn("ProtectSystem=strict", web)
        self.assertIn("ProtectHome=yes", web)
        self.assertIn("MemoryDenyWriteExecute=yes", web)
        self.assertIn("RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6", web)
        self.assertIn("VHACKINTOSH_WEB_PORT=8443", web)
        self.assertIn("ExecStartPre=+/usr/local/bin/vhackintosh-web-bootstrap", web)

    def test_vm_service_has_an_independent_systemd_lifecycle_and_cgroup(self):
        vm_path = AIROOTFS / "etc/systemd/system/vhackintosh-vm@.service"
        vm_text = vm_path.read_text(encoding="utf-8")
        agent_path = AIROOTFS / "etc/systemd/system/vhackintosh-agent.service"
        web_path = AIROOTFS / "etc/systemd/system/vhackintosh-web.service"
        vm = configparser.ConfigParser(strict=False, interpolation=None)
        vm.read_string(vm_path.read_text(encoding="utf-8"))
        agent = configparser.ConfigParser(strict=False)
        agent.read_string(agent_path.read_text(encoding="utf-8"))
        web = configparser.ConfigParser(strict=False)
        web.read_string(web_path.read_text(encoding="utf-8"))

        self.assertEqual(vm["Service"]["Type"], "simple")
        self.assertEqual(
            vm["Service"]["ExecStart"],
            "/usr/bin/python3 -m core.vm_supervisor --vm-id %i",
        )
        self.assertNotIn("openvt", vm["Service"]["ExecStart"])
        self.assertEqual(vm["Service"]["KillMode"], "control-group")
        self.assertIn("vhackintosh-display.service", vm["Unit"].get("Requires", ""))
        self.assertIn("vhackintosh-display.service", vm["Unit"].get("After", ""))
        self.assertEqual(vm["Service"]["Environment"].split("\\n")[-1], "WAYLAND_DISPLAY=wayland-0")
        self.assertIn("Environment=VHACKINTOSH_VM_ID=%i", vm_text)
        self.assertIn("Environment=VHACKINTOSH_VM_CONTROL_BASE=%t", vm_text)
        self.assertIn("Environment=VHACKINTOSH_VM_SUPERVISOR=1", vm_text)
        self.assertIn("Environment=VHACKINTOSH_CONFIG_DIR=/root/.config/vhackintosh", vm_text)
        self.assertIn("Environment=VHACKINTOSH_REIMS_REGISTRY=/root/.config/reims-ctl/vms.toml", vm_text)
        self.assertIn("Environment=XDG_CONFIG_HOME=/root/.config", vm_text)
        self.assertIn("Environment=XDG_DATA_HOME=/root/.local/share", vm_text)
        self.assertIn("Environment=CARGO_HOME=/root/.cargo", vm_text)
        self.assertIn("RuntimeDirectory=vh-%i", vm_text)
        for dependency in ("PartOf", "BindsTo", "JoinsNamespaceOf"):
            self.assertNotIn(dependency, vm["Unit"])
        for unit in (agent, web):
            self.assertNotIn("PartOf", unit["Unit"])
            self.assertNotIn("vhackintosh-vm@", unit["Unit"].get("Requires", ""))

    def test_web_identity_and_persistent_directories_are_declared(self):
        sysusers = (AIROOTFS / "usr/lib/sysusers.d/vhackintosh-web.conf").read_text(encoding="utf-8")
        tmpfiles = (AIROOTFS / "usr/lib/tmpfiles.d/vhackintosh-web.conf").read_text(encoding="utf-8")
        self.assertRegex(sysusers, r"(?m)^g vhackintosh-web -$")
        self.assertRegex(sysusers, r"(?m)^u vhackintosh-web - .* /usr/bin/nologin$")
        self.assertIn("/var/lib/vhackintosh/web 0750 vhackintosh-web vhackintosh-web", tmpfiles)
        self.assertIn("/var/lib/vhackintosh/web/tls 0700 vhackintosh-web vhackintosh-web", tmpfiles)

    def test_build_packages_only_locked_web_dependencies_and_built_frontend(self):
        build = (ROOT / "appliance/build-iso.sh").read_text(encoding="utf-8")
        self.assertIn("lib/package_web_manager.sh", build)
        self.assertIn("lib/sanitize_git_config.sh", build)
        for excluded in (
            "node_modules/", ".env*", ".npmrc", ".netrc", ".git-credentials",
            "credentials.json", "*.key", "*.pem", "*.p12", "*.pfx", "*.crt", "*.token",
        ):
            self.assertIn(f"--exclude='{excluded}'", build)
        self.assertIn("multi-user.target.wants", build)
        packager = (ROOT / "appliance/lib/package_web_manager.sh").read_text(encoding="utf-8")
        self.assertIn("npm ci", packager)
        self.assertIn("--ignore-scripts", packager)
        self.assertIn("--require-hashes", packager)
        self.assertIn("requirements.lock", packager)
        self.assertIn("node_modules", packager)
        self.assertIn(".npmrc", packager)
        lock = (ROOT / "web/requirements.lock").read_text(encoding="utf-8")
        for package in ("argon2-cffi", "fastapi", "psutil", "uvicorn"):
            self.assertRegex(lock, rf"(?m)^{re.escape(package)}==[^\\\s]+")
        self.assertGreaterEqual(lock.count("--hash=sha256:"), 20)

    def test_packaged_git_metadata_discards_checkout_credentials(self):
        with tempfile.TemporaryDirectory(prefix="vhack-safe-git-config-") as tmp:
            repository = Path(tmp) / "vhackintosh"
            repository.mkdir()
            subprocess.run(["git", "init", "--quiet", str(repository)], check=True)
            config = repository / ".git/config"
            with config.open("a", encoding="utf-8") as handle:
                handle.write(
                    '\n[credential]\n\thelper = store\n'
                    '[http "https://github.com"]\n\textraheader = AUTHORIZATION: bearer private-token\n'
                    '[remote "origin"]\n\turl = https://user:secret@example.invalid/repo.git\n'
                )
            subprocess.run(
                ["bash", str(ROOT / "appliance/lib/sanitize_git_config.sh"), str(repository)],
                check=True,
                capture_output=True,
                text=True,
            )
            values = subprocess.run(
                ["git", "-C", str(repository), "config", "--local", "--list"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertIn("remote.origin.url=https://github.com/felipeab10/vhackintosh.git", values)
            self.assertNotIn("credential.helper", values)
            self.assertNotIn("extraheader", values)
            self.assertNotIn("private-token", values)
            self.assertNotIn("user:secret", values)

    def test_installer_enables_web_services_and_preserves_installed_web_state(self):
        installer = (AIROOTFS / "usr/local/bin/vhackintosh-install").read_text(encoding="utf-8")
        copier = (AIROOTFS / "usr/local/bin/vhack-copy-system").read_text(encoding="utf-8")
        self.assertIn(
            "systemctl enable vhackintosh-agent.service vhackintosh-web.service",
            installer,
        )
        self.assertIn('SYSTEMD_WANTS_DIR="/mnt/etc/systemd/system/multi-user.target.wants"', installer)
        self.assertIn('ln -sfn ../vhackintosh-agent.service "${SYSTEMD_WANTS_DIR}/vhackintosh-agent.service"', installer)
        self.assertIn('ln -sfn ../vhackintosh-web.service "${SYSTEMD_WANTS_DIR}/vhackintosh-web.service"', installer)
        self.assertIn('systemctl enable vhackintosh-agent.service vhackintosh-web.service || true', installer)
        # Both copy paths exclude the live image's disposable credentials and
        # certificates while rsync's exclusion protects existing target data.
        self.assertIn("/var/lib/vhackintosh/web/***", copier)
        self.assertIn("/var/lib/vhackintosh/web/***", installer)
        self.assertIn("ExecStartPre=+/usr/local/bin/vhackintosh-web-bootstrap", (AIROOTFS / "etc/systemd/system/vhackintosh-web.service").read_text(encoding="utf-8"))
        server = (ROOT / "web/api/server.py").read_text(encoding="utf-8")
        self.assertIn("ensure_tls_material(args.tls_dir", server)

    @unittest.skipUnless(shutil.which("rsync"), "rsync is required to verify installer copy semantics")
    def test_rsync_copy_exclusion_preserves_existing_web_credentials(self):
        with tempfile.TemporaryDirectory(prefix="vhack-install-copy-test-") as tmp:
            root = Path(tmp)
            source = root / "live"
            destination = root / "installed"
            live_state = source / "var/lib/vhackintosh/web/auth.sqlite"
            installed_state = destination / "var/lib/vhackintosh/web/auth.sqlite"
            live_state.parent.mkdir(parents=True)
            installed_state.parent.mkdir(parents=True)
            live_state.write_text("disposable live state", encoding="utf-8")
            installed_state.write_text("existing installed state", encoding="utf-8")
            subprocess.run(
                [
                    "rsync", "-a", "--delete",
                    "--exclude=/var/lib/vhackintosh/web/***",
                    f"{source}/", f"{destination}/",
                ],
                check=True,
                capture_output=True,
            )
            self.assertEqual(installed_state.read_text(encoding="utf-8"), "existing installed state")

    def test_profile_reapplies_systemd_payload_permissions(self):
        profile = (ROOT / "appliance/archiso/profiledef.sh").read_text(encoding="utf-8")
        for path in (
            "/etc/systemd/system/vhackintosh-agent.service",
            "/etc/systemd/system/vhackintosh-web.service",
            "/etc/systemd/system/vhackintosh-vm@.service",
            "/etc/systemd/system/vhackintosh-display.service",
            "/usr/lib/sysusers.d/vhackintosh-web.conf",
            "/usr/lib/tmpfiles.d/vhackintosh-web.conf",
        ):
            with self.subTest(path=path):
                self.assertIn(f'["{path}"]="0:0:644"', profile)
        self.assertIn('["/usr/local/bin/vhackintosh-web-bootstrap"]="0:0:755"', profile)
        self.assertIn('["/usr/local/bin/vhackintosh-web-admin"]="0:0:755"', profile)
        self.assertIn('["/usr/local/bin/vhackintosh-web-recover"]="0:0:755"', profile)
        self.assertIn('["/usr/local/bin/vhackintosh-wait-wayland"]="0:0:755"', profile)

    def test_iso_validator_requires_payload_and_rejects_secrets_or_node_modules(self):
        required = (
            "opt/vhackintosh/web/current/web/api/server.py",
            "opt/vhackintosh/web/current/web/api/app.py",
            "opt/vhackintosh/web/current/web/api/admin_cli.py",
            "opt/vhackintosh/web/current/web/frontend/dist/index.html",
            "opt/vhackintosh/web/current/web/frontend/THIRD_PARTY_NOTICES.md",
            "opt/vhackintosh/web/current/web/frontend/licenses/inter-OFL-1.1.txt",
            "opt/vhackintosh/web/current/web/frontend/licenses/fira-code-OFL-1.1.txt",
            "opt/vhackintosh/web/current/web/frontend/licenses/lucide-ISC.txt",
            "opt/vhackintosh/web/current/requirements.lock",
            "opt/vhackintosh/web/current/core/web_jobs.py",
            "opt/vhackintosh/web/current/core/web_rpc.py",
            "opt/vhackintosh/web/current/core/runtime_inventory.py",
            "opt/vhackintosh/web/current/vendor/fastapi/__init__.py",
            "opt/vhackintosh/web/current/vendor/uvicorn/__init__.py",
            "opt/vhackintosh/web/current/vendor/argon2/__init__.py",
            "opt/vhackintosh/web/current/vendor/psutil/__init__.py",
            "etc/systemd/system/vhackintosh-agent.service",
            "etc/systemd/system/vhackintosh-web.service",
            "etc/systemd/system/vhackintosh-vm@.service",
            "etc/systemd/system/vhackintosh-display.service",
            "usr/local/bin/vhackintosh-wait-wayland",
            "usr/local/bin/vhackintosh-web-bootstrap",
            "usr/local/bin/vhackintosh-web-admin",
            "usr/local/bin/vhackintosh-web-recover",
            "usr/lib/sysusers.d/vhackintosh-web.conf",
            "usr/lib/tmpfiles.d/vhackintosh-web.conf",
            "etc/systemd/system/multi-user.target.wants/vhackintosh-agent.service",
            "etc/systemd/system/multi-user.target.wants/vhackintosh-web.service",
        )
        with tempfile.TemporaryDirectory(prefix="vhack-iso-web-test-") as tmp:
            rootfs = Path(tmp)
            bundle_root = rootfs / "opt/vhackintosh/web/bundles" / ("0" * 64)
            for relative in required:
                if relative.startswith("opt/vhackintosh/web/current/"):
                    relative = "opt/vhackintosh/web/bundles/" + ("0" * 64) + "/" + relative.removeprefix("opt/vhackintosh/web/current/")
                file = rootfs / relative
                if relative.startswith("etc/systemd/system/multi-user.target.wants/"):
                    file.parent.mkdir(parents=True, exist_ok=True)
                    file.symlink_to(f"../{file.name}")
                    continue
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text("test", encoding="utf-8")
                if relative == "usr/local/bin/vhackintosh-wait-wayland":
                    file.chmod(0o755)
            web_root = rootfs / "opt/vhackintosh/web"
            (rootfs / "opt/reims-vgpu/.release").parent.mkdir(parents=True, exist_ok=True)
            (rootfs / "opt/reims-vgpu/.release").write_text("v1.0.0\n", encoding="utf-8")
            from core.web_bundle import write_bundle_metadata
            metadata = write_bundle_metadata(bundle_root, runtime_release="v1.0.0")
            versioned = web_root / "bundles" / metadata.bundle_id
            if versioned != bundle_root:
                bundle_root.rename(versioned)
            (web_root / "current").symlink_to(f"bundles/{metadata.bundle_id}")
            validate_web_manager_payload(rootfs)
            wrong_service_link = rootfs / "etc/systemd/system/multi-user.target.wants/vhackintosh-web.service"
            wrong_service_link.unlink()
            wrong_service_link.symlink_to("/dev/null")
            with self.assertRaisesRegex(RuntimeError, "not enabled for boot"):
                validate_web_manager_payload(rootfs)
            wrong_service_link.unlink()
            wrong_service_link.symlink_to("../vhackintosh-web.service")
            tampered = web_root / "current/web/frontend/dist/index.html"
            tampered.write_text("modified after packaging", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "SHA-256 manifest does not cover every packaged file|hash mismatch"):
                validate_web_manager_payload(rootfs)
            tampered.write_text("test", encoding="utf-8")
            bad_modules = web_root / "current/web/frontend/node_modules"
            bad_modules.mkdir(parents=True)
            with self.assertRaisesRegex(RuntimeError, "developer-only"):
                validate_web_manager_payload(rootfs)
            bad_modules.rmdir()
            (web_root / "current/vendor/server.key").write_text("private", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "credential or TLS"):
                validate_web_manager_payload(rootfs)

    def test_bootstrap_helper_invalidates_legacy_code_and_announces_initial_credentials(self):
        helper = AIROOTFS / "usr/local/bin/vhackintosh-web-bootstrap"
        with tempfile.TemporaryDirectory(prefix="vhack-bootstrap-test-") as tmp:
            directory = Path(tmp)
            code_file = directory / "run/bootstrap-code"
            code_file.parent.mkdir()
            code_file.write_text("legacy-secret-code", encoding="ascii")
            master, slave = os.openpty()
            console = Path(f"/proc/self/fd/{slave}")
            try:
                command = [
                    sys.executable, str(helper), "--code-file", str(code_file),
                    "--database", str(directory / "auth.sqlite"),
                    "--console", str(console),
                ]
                first = subprocess.run(command, capture_output=True, check=False, pass_fds=(slave,))
                self.assertEqual(first.returncode, 0, first.stderr.decode(errors="replace"))
                printed = os.read(master, 8192).decode("utf-8", errors="replace")
                self.assertFalse(code_file.exists())
                self.assertIn("Usuário: admin", printed)
                self.assertIn("Senha provisória: vhackintosh", printed)
                self.assertIn("obrigatório definir uma nova senha", printed)
                self.assertNotIn("legacy-secret-code", printed + first.stdout.decode() + first.stderr.decode())
            finally:
                os.close(slave)
                os.close(master)


if __name__ == "__main__":
    unittest.main()
