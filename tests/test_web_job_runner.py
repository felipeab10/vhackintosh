import tempfile
import sys
import threading
import time
import unittest
from pathlib import Path

from core.web_job_runner import WebJobRunner
from core.web_jobs import JobStore


class WebJobRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = JobStore(Path(self.tmp.name) / "jobs.sqlite3")
        self.runners = []

    def tearDown(self):
        for runner in self.runners:
            runner.close()
        self.tmp.cleanup()

    def runner(self, **kwargs):
        runner = WebJobRunner(self.store, **kwargs)
        self.runners.append(runner)
        return runner

    def wait_for(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail("condition did not become true before timeout")

    def test_worker_concurrency_is_bounded(self):
        runner = self.runner(workers=2, queue_size=8)
        gate = threading.Event()
        lock = threading.Lock()
        active = 0
        maximum = 0
        finished = []

        def handler(payload, context):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            context.checkpoint("started", {"value": payload})
            gate.wait(2)
            with lock:
                active -= 1
                finished.append(payload)
            return payload

        runner.register("vm.provision", handler)
        runner.start()
        jobs = [runner.submit(idempotency_key=f"k-{i}", kind="vm.provision", payload=i)[0] for i in range(5)]
        self.wait_for(lambda: maximum == 2)
        gate.set()
        self.wait_for(lambda: all(self.store.get_job(job["id"])["state"] == "succeeded" for job in jobs))
        self.assertEqual(maximum, 2)
        self.assertCountEqual(finished, range(5))

    def test_idempotent_submit_and_explicit_retry_skip_completed_stage(self):
        runner = self.runner(workers=1)
        effects = []
        entered = threading.Event()
        release = threading.Event()
        attempts = 0

        def handler(payload, context):
            nonlocal attempts
            attempts += 1
            context.run_step("allocated", lambda: effects.append(payload["vm"]))
            if payload.get("fail_once") and attempts == 1:
                entered.set()
                release.wait(2)
                raise RuntimeError("temporary failure")
            return {"vm": payload["vm"]}

        runner.register("vm.provision", handler)
        runner.start()
        job, created = runner.submit(
            idempotency_key="create-1", kind="vm.provision", payload={"vm": "macos", "fail_once": True}
        )
        self.assertTrue(created)
        self.assertTrue(entered.wait(2))
        duplicate, created_again = runner.submit(
            idempotency_key="create-1", kind="vm.provision", payload={"vm": "macos", "fail_once": True}
        )
        self.assertFalse(created_again)
        self.assertEqual(job["id"], duplicate["id"])
        with self.assertRaises(ValueError):
            runner.submit(idempotency_key="create-1", kind="vm.provision", payload={"vm": "different"})
        release.set()
        self.wait_for(lambda: self.store.get_job(job["id"])["state"] == "failed")
        runner.retry(job["id"])
        self.wait_for(lambda: self.store.get_job(job["id"])["state"] == "succeeded")
        self.assertEqual(effects, ["macos"])

    def test_restart_reconciles_running_without_automatic_replay_then_explicit_retry_skips_effect(self):
        job, _ = self.store.create_job(idempotency_key="inflight", kind="vm.provision")
        self.store.set_checkpoint(job["id"], WebJobRunner._REQUEST_CHECKPOINT, {
            "payload": {"vm": "macos"},
            "fingerprint": "stored",
        })
        self.store.set_checkpoint(job["id"], "provisioned", {"status": "done"}, completed=True)
        self.store.transition(job["id"], "running", expected_state="queued")
        effects = []
        runner = self.runner(workers=1)
        def handler(payload, context):
            context.run_step("provisioned", lambda: effects.append(payload))
            return {"resumed": True}

        runner.register("vm.provision", handler)
        runner.start()
        self.assertEqual(self.store.get_job(job["id"])["state"], "interrupted")
        time.sleep(0.05)
        self.assertEqual(effects, [])
        runner.retry(job["id"])
        self.wait_for(lambda: self.store.get_job(job["id"])["state"] == "succeeded")
        self.assertEqual(effects, [])

    def test_queued_request_is_recovered_but_interrupted_requires_explicit_retry(self):
        job, _ = self.store.create_job(idempotency_key="queued", kind="vm.provision")
        self.store.set_checkpoint(job["id"], WebJobRunner._REQUEST_CHECKPOINT, {
            "payload": {"vm": "macos"},
            "fingerprint": "stored",
        })
        completed = threading.Event()
        runner = self.runner(workers=1)
        runner.register("vm.provision", lambda payload, context: completed.set() or payload)
        runner.start()
        self.assertTrue(completed.wait(2))
        self.wait_for(lambda: self.store.get_job(job["id"])["state"] == "succeeded")

    def test_checkpoint_timeout_fails_at_next_cooperative_boundary(self):
        runner = self.runner(workers=1, checkpoint_timeout=0.03)
        runner.register("vm.provision", lambda payload, context: (time.sleep(0.05), context.checkpoint("late")))
        runner.start()
        job, _ = runner.submit(idempotency_key="timeout", kind="vm.provision", payload={})
        self.wait_for(lambda: self.store.get_job(job["id"])["state"] == "failed")
        self.assertEqual(self.store.get_job(job["id"])["error_code"], "checkpoint_timeout")

    def test_supervised_subprocess_runs_argv_without_shell_and_times_out(self):
        runner = self.runner(workers=1, checkpoint_timeout=2)
        runner.register("process.ok", lambda _payload, context: context.run_subprocess(
            [sys.executable, "-c", "import sys; sys.exit(0)"], timeout=1,
        ).returncode)
        runner.register("process.timeout", lambda _payload, context: context.run_subprocess(
            [sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.1,
        ).returncode)
        runner.start()
        success, _ = runner.submit(idempotency_key="process-ok", kind="process.ok", payload={})
        self.wait_for(lambda: self.store.get_job(success["id"])["state"] == "succeeded")
        self.assertEqual(self.store.get_job(success["id"])["result"], 0)
        timed_out, _ = runner.submit(idempotency_key="process-timeout", kind="process.timeout", payload={})
        self.wait_for(lambda: self.store.get_job(timed_out["id"])["state"] == "failed")
        self.assertEqual(self.store.get_job(timed_out["id"])["error_code"], "checkpoint_timeout")

    def test_supervised_subprocess_cancels_and_runner_shutdown_reconciles(self):
        runner = self.runner(workers=1, checkpoint_timeout=5)
        entered = threading.Event()
        runner.register("process.cancel", lambda _payload, context: (
            entered.set(), context.run_subprocess(
                [sys.executable, "-c", "import time; time.sleep(5)"], timeout=4,
            ),
        ))
        runner.register("process.shutdown", lambda _payload, context: context.run_subprocess(
            [sys.executable, "-c", "import time; time.sleep(5)"], timeout=4,
        ))
        runner.start()
        cancelled, _ = runner.submit(idempotency_key="process-cancel", kind="process.cancel", payload={})
        self.assertTrue(entered.wait(2))
        runner.cancel(cancelled["id"])
        self.wait_for(lambda: self.store.get_job(cancelled["id"])["state"] == "cancelled")
        interrupted, _ = runner.submit(idempotency_key="process-shutdown", kind="process.shutdown", payload={})
        self.wait_for(lambda: self.store.get_job(interrupted["id"])["state"] == "running")
        runner.close(wait=True)
        self.assertEqual(self.store.get_job(interrupted["id"])["state"], "interrupted")


if __name__ == "__main__":
    unittest.main()
