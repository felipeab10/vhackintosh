from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from web.api.transfers import (
    InvalidTransfer,
    PrivateTransferStore,
    TransferQuotaExceeded,
    TransferTooLarge,
    TransferNotFound,
)


def valid_iso() -> bytes:
    data = bytearray(32775)
    data[16 * 2048 : 16 * 2048 + 7] = b"\x01CD001\x01"
    return bytes(data)


class PrivateTransferStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "private"

    def tearDown(self):
        self.temp.cleanup()

    def upload_iso(self, store: PrivateTransferStore, name: str = "recovery.iso", content: bytes | None = None):
        writer = store.begin(name, "iso")
        writer.write(valid_iso() if content is None else content)
        return writer.commit()

    def test_streamed_iso_is_staged_atomically_and_downloadable(self):
        store = PrivateTransferStore(self.root, max_file_bytes=40_000, max_total_bytes=80_000)
        writer = store.begin("recovery.iso", "iso")
        payload = valid_iso()
        writer.write(payload[:15000])
        writer.write(payload[15000:])
        transfer = writer.commit()
        self.assertEqual(transfer.size_bytes, len(payload))
        self.assertEqual(store.list(), [transfer])
        saved, source = store.open_download(transfer.id)
        with source:
            self.assertEqual(source.read(), payload)
        self.assertEqual(saved.filename, "recovery.iso")
        self.assertTrue((self.root / f"{transfer.id}--recovery.iso").is_file())
        self.assertEqual(list(self.root.glob("*.part")), [])

    def test_explicit_qcow2_and_udif_signatures_are_accepted(self):
        store = PrivateTransferStore(self.root, max_file_bytes=1024, max_total_bytes=2048, min_free_bytes=0)
        qcow = b"QFI\xfb" + b"\x00" * 68
        writer = store.begin("disk.qcow2", "qcow2")
        writer.write(qcow)
        self.assertEqual(writer.commit().format, "qcow2")
        dmg = bytearray(512)
        dmg[:4] = b"koly"
        writer = store.begin("installer.dmg", "dmg")
        writer.write(bytes(dmg))
        self.assertEqual(writer.commit().format, "dmg")

    def test_rejects_traversal_symlink_root_and_unsupported_or_misidentified_formats(self):
        store = PrivateTransferStore(self.root, max_file_bytes=40_000, max_total_bytes=80_000)
        for name in ("../escape.iso", "..\\escape.iso", "/tmp/escape.iso", "ok.qcow2"):
            with self.subTest(name=name):
                with self.assertRaises(InvalidTransfer):
                    store.begin(name, "iso")
        with self.assertRaises(InvalidTransfer):
            store.begin("unknown.raw", "raw")
        writer = store.begin("not-really.iso", "iso")
        writer.write(b"x" * 32775)
        with self.assertRaises(InvalidTransfer):
            writer.commit()
        self.assertEqual(store.list(), [])
        self.assertEqual(list(self.root.glob("*.part")), [])

        link_target = Path(self.temp.name) / "actual"
        link_target.mkdir()
        symlink_root = Path(self.temp.name) / "linked"
        symlink_root.symlink_to(link_target, target_is_directory=True)
        with self.assertRaises(ValueError):
            PrivateTransferStore(symlink_root).list()

    def test_enforces_file_and_total_quotas_and_removes_only_partial_file(self):
        store = PrivateTransferStore(self.root, max_file_bytes=32774, max_total_bytes=32774)
        writer = store.begin("recovery.iso", "iso")
        with self.assertRaises(TransferTooLarge):
            writer.write(valid_iso())
        writer.abort()
        self.assertEqual(list(self.root.glob("*.part")), [])
        self.assertEqual([entry.name for entry in self.root.iterdir()], [".quota.lock"])

        store = PrivateTransferStore(Path(self.temp.name) / "small", max_file_bytes=32775, max_total_bytes=32775)
        self.upload_iso(store)
        with self.assertRaises(TransferQuotaExceeded):
            store.begin("other.iso", "iso")
        self.assertEqual(len(store.list()), 1)

    def test_reconciles_crashed_stages_but_keeps_upload_owned_by_live_writer(self):
        store = PrivateTransferStore(self.root, max_file_bytes=40_000, max_total_bytes=80_000)
        writer = store.begin("in-progress.iso", "iso")
        writer.write(b"partial upload")
        active_stage = writer.stage

        # A second API worker may list or enforce quota while the first worker
        # is streaming; the live writer's owner lock protects its partial file.
        concurrent_store = PrivateTransferStore(self.root, max_file_bytes=40_000, max_total_bytes=80_000)
        self.assertEqual(concurrent_store.list(), [])
        self.assertTrue(active_stage.is_file())

        writer.abort()
        stale_id = "a" * 32
        stale_stage = self.root / f".{stale_id}.part"
        stale_owner = self.root / f".{stale_id}.active"
        stale_stage.write_bytes(b"orphaned after process crash")
        stale_owner.touch(mode=0o600)

        restarted_store = PrivateTransferStore(self.root, max_file_bytes=40_000, max_total_bytes=80_000)
        self.assertEqual(restarted_store.list(), [])
        self.assertFalse(stale_stage.exists())
        self.assertFalse(stale_owner.exists())

    def test_counts_orphan_staging_files_and_preserves_reserved_host_space(self):
        store = PrivateTransferStore(self.root, max_file_bytes=40_000, max_total_bytes=40_000, min_free_bytes=1)
        self.root.mkdir(mode=0o700)
        (self.root / ".interrupted.part").write_bytes(b"stale temporary data")
        writer = store.begin("recovery.iso", "iso")
        with patch("web.api.transfers.shutil.disk_usage") as disk_usage:
            disk_usage.return_value.free = 100
            with self.assertRaises(TransferQuotaExceeded):
                writer.write(b"x" * 32775)
        writer.abort()
        self.assertTrue((self.root / ".interrupted.part").exists())
        self.assertEqual(store.list(), [])

    def test_deletes_only_the_requested_transfer_and_missing_ids_stay_private(self):
        store = PrivateTransferStore(self.root, max_file_bytes=40_000, max_total_bytes=80_000)
        item = self.upload_iso(store)
        store.delete(item.id)
        self.assertEqual(store.list(), [])
        with self.assertRaises(TransferNotFound):
            store.open_download(item.id)

    def test_symlinked_transfer_entry_is_not_listed_or_downloaded(self):
        store = PrivateTransferStore(self.root, max_file_bytes=40_000, max_total_bytes=80_000)
        self.root.mkdir(mode=0o700)
        target = Path(self.temp.name) / "outside.iso"
        target.write_bytes(valid_iso())
        transfer_id = "a" * 32
        (self.root / f"{transfer_id}--outside.iso").symlink_to(target)

        self.assertEqual(store.list(), [])
        with self.assertRaises(TransferNotFound):
            store.open_download(transfer_id)


if __name__ == "__main__":
    unittest.main()
