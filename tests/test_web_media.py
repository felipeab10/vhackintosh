from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.web_media import (
    InvalidMediaTransfer,
    ManagedMediaImporter,
    MediaAlreadyExists,
    MediaTooLarge,
    MediaTransferNotFound,
)


def valid_iso() -> bytes:
    data = bytearray(32775)
    data[16 * 2048 : 16 * 2048 + 7] = b"\x01CD001\x01"
    return bytes(data)


class ManagedMediaImporterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.spool = self.base / "spool"
        self.media = self.base / "media"
        self.transfer_id = "a" * 32
        self.source = self.spool / f"{self.transfer_id}--Recovery.iso"
        self.spool.mkdir()
        self.source.write_bytes(valid_iso())

    def tearDown(self):
        self.temp.cleanup()

    def importer(self, **kwargs):
        return ManagedMediaImporter(self.spool, self.media, **kwargs)

    def test_imports_valid_media_in_bounded_chunks_and_preserves_private_upload(self):
        self.spool.chmod(0o750)
        importer = self.importer(max_file_bytes=40000, chunk_bytes=4096)
        result = importer.import_transfer(self.transfer_id)

        self.assertEqual(result.id, self.transfer_id)
        self.assertEqual(result.name, "Recovery.iso")
        self.assertEqual(result.format, "iso")
        self.assertEqual(result.size_bytes, len(valid_iso()))
        self.assertFalse(hasattr(result, "path"))
        self.assertEqual((self.media / result.name).read_bytes(), valid_iso())
        self.assertTrue(self.source.is_file(), "Import must leave the original upload intact")
        self.assertEqual(self.spool.stat().st_mode & 0o777, 0o750, "The privileged importer must not change spool permissions")
        self.assertEqual(list(self.media.iterdir()), [self.media / result.name])

    def test_retry_after_published_import_is_idempotent(self):
        importer = self.importer(max_file_bytes=40000, chunk_bytes=4096)
        first = importer.import_transfer(self.transfer_id)
        second = importer.import_transfer(self.transfer_id)
        self.assertEqual(first, second)
        self.assertEqual(list(self.media.iterdir()), [self.media / "Recovery.iso"])

    def test_import_start_removes_only_orphaned_regular_staging_files(self):
        self.media.mkdir()
        orphan = self.media / (".import-" + "b" * 32 + ".tmp")
        orphan.write_bytes(b"partial copy")
        link = self.media / (".import-" + "c" * 32 + ".tmp")
        outside = self.base / "outside"
        outside.write_bytes(b"preserve")
        link.symlink_to(outside)

        self.importer(max_file_bytes=40000).import_transfer(self.transfer_id)

        self.assertFalse(orphan.exists())
        self.assertTrue(link.is_symlink())
        self.assertEqual(outside.read_bytes(), b"preserve")

    def test_accepts_the_same_qcow2_and_udif_signatures_as_private_transfers(self):
        for transfer_id, name, fmt, content in (
            ("b" * 32, "disk.qcow2", "qcow2", b"QFI\xfb" + b"\x00" * 68),
            ("c" * 32, "installer.dmg", "dmg", b"koly" + b"\x00" * 508),
        ):
            with self.subTest(format=fmt):
                source = self.spool / f"{transfer_id}--{name}"
                source.write_bytes(content)
                result = self.importer(max_file_bytes=1024).import_transfer(transfer_id)
                self.assertEqual((result.id, result.name, result.format, result.size_bytes),
                                 (transfer_id, name, fmt, len(content)))
                self.assertEqual((self.media / name).read_bytes(), content)

    def test_rejects_invalid_transfer_ids_without_searching_paths(self):
        for value in ("../" + self.transfer_id, self.transfer_id.upper(), "a" * 31, None):
            with self.subTest(value=value), self.assertRaises(InvalidMediaTransfer):
                self.importer().import_transfer(value)

    def test_missing_transfer_is_reported(self):
        with self.assertRaises(MediaTransferNotFound):
            self.importer().import_transfer("b" * 32)

    def test_rejects_symlinked_upload(self):
        self.source.unlink()
        external = self.base / "external.iso"
        external.write_bytes(valid_iso())
        self.source.symlink_to(external)
        with self.assertRaises(InvalidMediaTransfer):
            self.importer().import_transfer(self.transfer_id)
        self.assertEqual(list(self.media.iterdir()), [])

    def test_rejects_empty_and_invalid_signature(self):
        self.source.write_bytes(b"")
        with self.assertRaises(InvalidMediaTransfer):
            self.importer().import_transfer(self.transfer_id)
        self.source.write_bytes(b"x" * 32775)
        with self.assertRaises(InvalidMediaTransfer):
            self.importer().import_transfer(self.transfer_id)
        self.assertEqual(list(self.media.iterdir()), [])

    def test_rejects_name_collision_without_replacing_existing_media(self):
        self.media.mkdir()
        destination = self.media / "Recovery.iso"
        destination.write_bytes(b"existing")
        with self.assertRaises(MediaAlreadyExists):
            self.importer().import_transfer(self.transfer_id)
        self.assertEqual(destination.read_bytes(), b"existing")
        self.assertTrue(self.source.exists())
        self.assertEqual(list(self.media.iterdir()), [destination])

    def test_enforces_configured_size_limit(self):
        with self.assertRaises(MediaTooLarge):
            self.importer(max_file_bytes=1024).import_transfer(self.transfer_id)
        self.assertEqual(list(self.media.iterdir()), [])
        self.assertTrue(self.source.exists())

    def test_cleans_staging_file_when_copy_validation_fails(self):
        importer = self.importer(max_file_bytes=40000)
        validate = importer._validate_signature
        calls = 0

        def fail_after_copy(fd, fmt, size):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise InvalidMediaTransfer("simulated staged-file validation failure")
            validate(fd, fmt, size)

        with patch.object(importer, "_validate_signature", side_effect=fail_after_copy):
            with self.assertRaises(InvalidMediaTransfer):
                importer.import_transfer(self.transfer_id)
        self.assertEqual(list(self.media.iterdir()), [])
        self.assertTrue(self.source.exists())

    def test_cancellation_during_copy_removes_staging_and_never_publishes(self):
        checks = 0

        def cancel_after_two_chunks():
            nonlocal checks
            checks += 1
            if checks == 3:
                raise RuntimeError("job cancelled")

        importer = self.importer(max_file_bytes=40000, chunk_bytes=4096, check_cancelled=cancel_after_two_chunks)
        with self.assertRaisesRegex(RuntimeError, "job cancelled"):
            importer.import_transfer(self.transfer_id)

        self.assertEqual(list(self.media.iterdir()), [])
        self.assertTrue(self.source.is_file())

    def test_cancellation_at_publish_boundary_never_publishes_completed_staging(self):
        checks = 0

        def cancel_before_publish():
            nonlocal checks
            checks += 1
            # 9 chunks plus the EOF check for the fixture, then the final
            # publish-boundary check.
            if checks == 11:
                raise RuntimeError("job cancelled")

        importer = self.importer(max_file_bytes=40000, chunk_bytes=4096, check_cancelled=cancel_before_publish)
        with self.assertRaisesRegex(RuntimeError, "job cancelled"):
            importer.import_transfer(self.transfer_id)

        self.assertEqual(list(self.media.iterdir()), [])
        self.assertTrue(self.source.is_file())


if __name__ == "__main__":
    unittest.main()
