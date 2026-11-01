import tempfile
import unittest
from pathlib import Path

from core.web_jobs import JobStore


class JobStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "ops.sqlite3"
        self.store = JobStore(self.db, log_limit_bytes=512)

    def tearDown(self):
        self.tmp.cleanup()

    def test_migration_idempotency_and_request_mismatch(self):
        job, created = self.store.create_job(
            idempotency_key="create-vm-1", kind="vm.create", target_vm_id="macos-1", vm_revision="r1"
        )
        duplicate, created_again = JobStore(self.db).create_job(
            idempotency_key="create-vm-1", kind="vm.create", target_vm_id="macos-1", vm_revision="r1"
        )
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(job["id"], duplicate["id"])
        with self.assertRaises(ValueError):
            self.store.create_job(
                idempotency_key="create-vm-1", kind="vm.delete", target_vm_id="macos-1", vm_revision="r1"
            )
        import sqlite3
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_transitions_progress_cancel_and_restart_reconciliation(self):
        job, _ = self.store.create_job(idempotency_key="install-1", kind="vm.install")
        job = self.store.transition(job["id"], "running", expected_state="queued")
        self.assertIsNotNone(job["started_at"])
        self.store.update_progress(job["id"], "download", 33.5)
        self.assertEqual(self.store.get_job(job["id"])["progress"], 33.5)
        job = self.store.request_cancel(job["id"])
        self.assertEqual(job["state"], "cancelling")
        self.assertTrue(job["cancel_requested"])
        self.assertEqual(self.store.reconcile_after_restart(), [job["id"]])
        recovered = self.store.get_job(job["id"])
        self.assertEqual(recovered["state"], "interrupted")
        self.assertEqual(recovered["phase"], "interrupted")

    def test_scoped_restart_reconciliation_leaves_other_worker_kinds_running(self):
        vm_job, _ = self.store.create_job(idempotency_key="vm-restart", kind="vm.provision")
        media_job, _ = self.store.create_job(idempotency_key="media-restart", kind="media.import")
        self.store.transition(vm_job["id"], "running", expected_state="queued")
        self.store.transition(media_job["id"], "running", expected_state="queued")

        self.assertEqual(self.store.reconcile_after_restart({"media.import"}), [media_job["id"]])
        self.assertEqual(self.store.get_job(vm_job["id"])["state"], "running")
        self.assertEqual(self.store.reconcile_after_restart({"vm.provision"}), [vm_job["id"]])

    def test_checkpoints_are_upserted_and_persisted(self):
        job, _ = self.store.create_job(idempotency_key="provision-1", kind="vm.provision")
        self.store.set_checkpoint(job["id"], "download", {"url": "https://example.invalid/image", "bytes": 128})
        self.store.set_checkpoint(job["id"], "download", {"bytes": 256}, completed=True)
        checkpoints = self.store.get_checkpoints(job["id"])
        self.assertEqual(len(checkpoints), 1)
        self.assertEqual(checkpoints[0]["payload"], {"bytes": 256})
        self.assertTrue(checkpoints[0]["completed"])

    def test_logs_remain_within_per_job_byte_limit_and_order(self):
        job, _ = self.store.create_job(idempotency_key="logs-1", kind="vm.create")
        for i in range(12):
            self.store.append_log(job["id"], f"log-{i}-" + ("x" * 100))
        logs = self.store.get_logs(job["id"])
        self.assertTrue(logs)
        self.assertIn("log-11-", logs[-1]["message"])
        self.assertEqual(logs, sorted(logs, key=lambda entry: entry["id"]))
        self.assertLessEqual(sum(len(entry["message"].encode()) for entry in logs), 512)

    def test_rejects_invalid_state_changes_and_missing_targets(self):
        job, _ = self.store.create_job(idempotency_key="bad-1", kind="vm.create")
        with self.assertRaises(ValueError):
            self.store.transition(job["id"], "succeeded")
        with self.assertRaises(ValueError):
            self.store.update_progress(job["id"], "not-started", 10)
        with self.assertRaises(KeyError):
            self.store.append_log("missing", "hello")


if __name__ == "__main__":
    unittest.main()
