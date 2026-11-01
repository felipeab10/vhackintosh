from __future__ import annotations

import json
import stat
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.namespace_migration import (
    NamespaceMigrationError,
    migrate_namespace_offline,
    preview_namespace_migration,
)


class NamespaceMigrationTests(unittest.TestCase):
    def make_legacy(self, root: Path) -> None:
        (root / "vms" / "legacy-vm").mkdir(parents=True)
        (root / "vms.json").write_text(
            json.dumps({"vms": {"legacy-vm": {"id": "legacy-vm", "disk_path": "/external/mac.qcow2"}}}),
            encoding="utf-8",
        )
        (root / "settings.json").write_text('{"language":"pt_BR"}\n', encoding="utf-8")
        (root / "vms" / "legacy-vm" / "vm.toml").write_text('name = "legacy-vm"\n', encoding="utf-8")
        (root / "vms" / "legacy-vm" / "mac.qcow2").write_bytes(b"VM DISK - DO NOT COPY")

    def test_preview_is_read_only_and_limits_itself_to_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, destination = root / "old", root / "new"
            self.make_legacy(source)
            before = {path.relative_to(source): path.read_bytes() for path in source.rglob("*") if path.is_file()}

            plan = preview_namespace_migration(source, destination)

            self.assertEqual({item.relative_path for item in plan.files}, {
                "vms.json", "settings.json", "vms/legacy-vm/vm.toml",
            })
            self.assertFalse(destination.exists())
            self.assertEqual(before, {path.relative_to(source): path.read_bytes() for path in source.rglob("*") if path.is_file()})

    def test_offline_migration_backs_up_verifies_and_preserves_source_and_disk(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, destination = root / "old", root / "new"
            self.make_legacy(source)
            original = (source / "vms.json").read_bytes()

            plan = migrate_namespace_offline(
                source, destination, runtime_state="stopped", services_stopped=True,
                runtime_state_reader=lambda: {},
            )

            self.assertFalse(plan.already_migrated)
            self.assertEqual((destination / "vms.json").read_bytes(), original)
            self.assertEqual(
                (destination / ".vhackintosh-migration-backup/vms.json").read_bytes(), original,
            )
            self.assertEqual((source / "vms.json").read_bytes(), original)
            self.assertTrue((source / "vms/legacy-vm/mac.qcow2").exists())
            self.assertFalse((destination / "vms/legacy-vm/mac.qcow2").exists())
            marker = json.loads((destination / ".vhackintosh-namespace-migration.json").read_text())
            self.assertTrue(marker["source_retained"])
            self.assertEqual(stat.S_IMODE((destination / "vms").stat().st_mode), 0o700)

            repeated = migrate_namespace_offline(
                source, destination, runtime_state="stopped", services_stopped=True,
                runtime_state_reader=lambda: {},
            )
            self.assertTrue(repeated.already_migrated)

    def test_external_assets_identity_and_home_siblings_are_untouched(self):
        """The migration projects metadata only; it must not chmod HOME or move VM assets."""
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            source = home / ".local/share/vhackintosh"
            destination = home / ".local/share/vhackintosh-web"
            assets = home / "VMs/legacy-vm"
            protected = home / ".ssh/authorized_keys"
            (source / "vms/legacy-vm").mkdir(parents=True)
            assets.mkdir(parents=True)
            protected.parent.mkdir(parents=True)

            disk = assets / "macOS.qcow2"
            efi = assets / "OpenCore.qcow2"
            nvram = assets / "OVMF_VARS.fd"
            disk.write_bytes(b"external disk bytes")
            efi.write_bytes(b"external EFI bytes")
            nvram.write_bytes(b"external NVRAM bytes")
            protected.write_bytes(b"ssh key data")
            for path in (disk, efi, nvram):
                path.chmod(0o640)
            protected.chmod(0o600)

            registry = {
                "vms": {
                    "legacy-vm": {
                        "id": "legacy-vm",
                        "disk_path": str(disk),
                        "efi_path": str(efi),
                        "nvram_path": str(nvram),
                        "mac": "52:54:00:12:34:56",
                        "smbios": {"serial_number": "SERIAL-KEEP", "smuuid": "UUID-KEEP"},
                    }
                }
            }
            vms_json = source / "vms.json"
            vms_json.write_text(json.dumps(registry, sort_keys=True), encoding="utf-8")
            vm_toml = source / "vms/legacy-vm/vm.toml"
            vm_toml_bytes = (
                f'name = "legacy-vm"\nmac = "52:54:00:12:34:56"\n'
                f'disk = "{disk}"\nefi = "{efi}"\nnvram = "{nvram}"\n'
                'smbios_serial = "SERIAL-KEEP"\nsmbios_uuid = "UUID-KEEP"\n'
            ).encode()
            vm_toml.write_bytes(vm_toml_bytes)
            external_snapshot = {
                path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
                for path in (disk, efi, nvram, protected)
            }
            source_snapshot = {path: path.read_bytes() for path in (vms_json, vm_toml)}
            home_mode = stat.S_IMODE(home.stat().st_mode)
            source_mode = stat.S_IMODE(source.stat().st_mode)

            result = migrate_namespace_offline(
                source,
                destination,
                runtime_state="stopped",
                services_stopped=True,
                runtime_state_reader=lambda: {},
            )

            self.assertFalse(result.already_migrated)
            for path, (content, mode) in external_snapshot.items():
                self.assertEqual(path.read_bytes(), content)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode)
            self.assertEqual({path: path.read_bytes() for path in source_snapshot}, source_snapshot)
            self.assertEqual(stat.S_IMODE(home.stat().st_mode), home_mode)
            self.assertEqual(stat.S_IMODE(source.stat().st_mode), source_mode)
            self.assertFalse((destination / "VMs").exists())
            self.assertFalse((destination / "vms/legacy-vm/macOS.qcow2").exists())
            self.assertEqual((destination / "vms.json").read_bytes(), source_snapshot[vms_json])
            self.assertEqual((destination / "vms/legacy-vm/vm.toml").read_bytes(), vm_toml_bytes)

    def test_failed_namespace_publication_keeps_identity_assets_and_source_for_rollback(self):
        """A failed projection leaves the old namespace authoritative and recoverable."""
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            source, destination = home / "legacy", home / "web"
            external = home / "external-vm"
            (source / "vms/legacy-vm").mkdir(parents=True)
            external.mkdir()
            disk, efi, nvram = external / "disk.qcow2", external / "OpenCore.qcow2", external / "OVMF_VARS.fd"
            disk.write_bytes(b"disk-preserve")
            efi.write_bytes(b"efi-preserve")
            nvram.write_bytes(b"nvram-preserve")
            metadata = {
                "vms": {
                    "legacy-vm": {
                        "disk_path": str(disk), "efi_path": str(efi), "nvram_path": str(nvram),
                        "mac": "52:54:00:AA:BB:CC",
                        "smbios": {"serial_number": "ROLLBACK-SERIAL", "smuuid": "ROLLBACK-UUID"},
                    }
                }
            }
            catalog = source / "vms.json"
            catalog.write_text(json.dumps(metadata, sort_keys=True), encoding="utf-8")
            vm_config = source / "vms/legacy-vm/vm.toml"
            vm_config.write_text(
                f'disk = "{disk}"\nefi = "{efi}"\nnvram = "{nvram}"\n'
                'mac = "52:54:00:AA:BB:CC"\nsmbios_serial = "ROLLBACK-SERIAL"\n'
                'smbios_uuid = "ROLLBACK-UUID"\n',
                encoding="utf-8",
            )
            original_metadata = {path: path.read_bytes() for path in (catalog, vm_config)}
            external_snapshot = {path: path.read_bytes() for path in (disk, efi, nvram)}

            with patch(
                "core.namespace_migration._publish_without_replace",
                side_effect=NamespaceMigrationError("simulated publication interruption"),
            ):
                with self.assertRaisesRegex(NamespaceMigrationError, "simulated publication interruption"):
                    migrate_namespace_offline(
                        source,
                        destination,
                        runtime_state="stopped",
                        services_stopped=True,
                        runtime_state_reader=lambda: {},
                    )

            self.assertFalse(destination.exists())
            self.assertEqual({path: path.read_bytes() for path in original_metadata}, original_metadata)
            self.assertEqual({path: path.read_bytes() for path in external_snapshot}, external_snapshot)
            staged = list(home.glob(".web.migration-*"))
            self.assertEqual(len(staged), 1)
            self.assertEqual((staged[0] / ".vhackintosh-migration-backup/vms.json").read_bytes(), original_metadata[catalog])
            self.assertEqual(
                (staged[0] / ".vhackintosh-migration-backup/vms/legacy-vm/vm.toml").read_bytes(),
                original_metadata[vm_config],
            )

    def test_running_unknown_or_service_active_blocks_without_mutation(self):
        for state, stopped, observed in (
            ("running", True, {}), ("unknown", True, None), ("stopped", False, {}),
            ("stopped", True, {"legacy-vm": 1234}),
        ):
            with self.subTest(state=state, stopped=stopped, observed=observed), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source, destination = root / "old", root / "new"
                self.make_legacy(source)
                with self.assertRaises(NamespaceMigrationError):
                    migrate_namespace_offline(
                        source, destination, runtime_state=state, services_stopped=stopped,
                        runtime_state_reader=lambda observed=observed: observed,
                    )
                self.assertFalse(destination.exists())
                self.assertTrue((source / "vms/legacy-vm/mac.qcow2").exists())

    def test_refuses_existing_destination_without_marker_and_stale_preview(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, destination = root / "old", root / "new"
            self.make_legacy(source)
            stale = preview_namespace_migration(source, destination)
            (source / "settings.json").write_text('{"language":"en"}\n', encoding="utf-8")
            with self.assertRaises(NamespaceMigrationError):
                migrate_namespace_offline(
                    source, destination, runtime_state="stopped", services_stopped=True, preview=stale,
                )
            self.assertFalse(destination.exists())
            destination.mkdir()
            with self.assertRaises(NamespaceMigrationError):
                preview_namespace_migration(source, destination)
            self.assertEqual(list(destination.iterdir()), [])

    def test_refuses_symlinked_metadata_without_reading_or_copying_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, destination = root / "old", root / "new"
            source.mkdir()
            outside = root / "outside.json"
            outside.write_text('{"secret":"must not be read"}')
            (source / "vms.json").symlink_to(outside)
            with self.assertRaises(NamespaceMigrationError):
                preview_namespace_migration(source, destination)
            self.assertFalse(destination.exists())

    def test_optional_reims_registry_is_backed_up_copied_and_never_rewritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, destination = root / "old", root / "new"
            reims_config = root / "legacy-reims" / "vms.toml"
            self.make_legacy(source)
            reims_config.parent.mkdir()
            registry_bytes = b'[[vm]]\nname = "legacy-vm"\ndir = "/external/unchanged-vm-dir"\n'
            reims_config.write_bytes(registry_bytes)
            before_registry = reims_config.read_bytes()
            plan = preview_namespace_migration(source, destination, reims_registry_source=reims_config)

            result = migrate_namespace_offline(
                source, destination, runtime_state="stopped", services_stopped=True,
                preview=plan, runtime_state_reader=lambda: {},
            )

            self.assertIn("reims-ctl/vms.toml", {item.relative_path for item in result.files})
            self.assertEqual((destination / "reims-ctl/vms.toml").read_bytes(), registry_bytes)
            self.assertEqual(
                (destination / ".vhackintosh-migration-backup/reims-ctl/vms.toml").read_bytes(),
                registry_bytes,
            )
            self.assertEqual(reims_config.read_bytes(), before_registry)
            self.assertEqual(reims_config.read_bytes(), registry_bytes)
            marker = json.loads((destination / ".vhackintosh-namespace-migration.json").read_text())
            self.assertEqual(marker["reims_registry_source"], str(reims_config))

            repeated = migrate_namespace_offline(
                source, destination, runtime_state="stopped", services_stopped=True,
                reims_registry_source=reims_config, runtime_state_reader=lambda: {},
            )
            self.assertTrue(repeated.already_migrated)

    def test_registry_path_must_be_absolute_regular_and_not_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, destination = root / "old", root / "new"
            self.make_legacy(source)
            registry = root / "vms.toml"
            registry.write_text('[[vm]]\nname = "id"\ndir = "/unchanged"\n')
            with self.assertRaises(NamespaceMigrationError):
                preview_namespace_migration(source, destination, reims_registry_source="relative/vms.toml")
            symlink = root / "registry-link.toml"
            symlink.symlink_to(registry)
            with self.assertRaises(NamespaceMigrationError):
                preview_namespace_migration(source, destination, reims_registry_source=symlink)
            self.assertFalse(destination.exists())

    def test_registry_change_after_preview_invalidates_plan(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, destination = root / "old", root / "new"
            registry = root / "vms.toml"
            self.make_legacy(source)
            registry.write_text('[[vm]]\nname = "id"\ndir = "/before"\n')
            plan = preview_namespace_migration(source, destination, reims_registry_source=registry)
            registry.write_text('[[vm]]\nname = "id"\ndir = "/after"\n')
            with self.assertRaises(NamespaceMigrationError):
                migrate_namespace_offline(
                    source, destination, runtime_state="stopped", services_stopped=True,
                    preview=plan, runtime_state_reader=lambda: {},
                )
            self.assertFalse(destination.exists())
            self.assertEqual(registry.read_text().split('dir = ')[1].strip(), '"/after"')

    def test_refuses_broken_metadata_symlink_and_broken_vm_toml(self):
        for relative in ("vms.json", "vms/legacy-vm/vm.toml"):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source, destination = root / "old", root / "new"
                self.make_legacy(source)
                target = source / relative
                target.unlink()
                target.symlink_to(source / "missing-target")
                with self.assertRaises(NamespaceMigrationError):
                    preview_namespace_migration(source, destination)
                self.assertFalse(destination.exists())

    def test_refuses_broken_vms_directory_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, destination = root / "old", root / "new"
            source.mkdir()
            (source / "vms").symlink_to(source / "missing-vms")
            with self.assertRaises(NamespaceMigrationError):
                preview_namespace_migration(source, destination)
            self.assertFalse(destination.exists())

    def test_rejects_new_metadata_appearing_during_copy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, destination = root / "old", root / "new"
            self.make_legacy(source)
            (source / "settings.json").unlink()
            original_copy = shutil.copy2
            created = False

            def copy_then_add_metadata(*args, **kwargs):
                nonlocal created
                result = original_copy(*args, **kwargs)
                if not created:
                    (source / "settings.json").write_text('{"added_during_copy":true}\n', encoding="utf-8")
                    created = True
                return result

            with patch("core.namespace_migration.shutil.copy2", side_effect=copy_then_add_metadata):
                with self.assertRaises(NamespaceMigrationError):
                    migrate_namespace_offline(
                        source, destination, runtime_state="stopped", services_stopped=True,
                        runtime_state_reader=lambda: {},
                    )
            self.assertFalse(destination.exists())
            self.assertTrue((source / "vms/legacy-vm/mac.qcow2").exists())


if __name__ == "__main__":
    unittest.main()
