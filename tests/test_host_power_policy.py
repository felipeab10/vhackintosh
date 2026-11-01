import unittest

from core.host_power_policy import evaluate_host_power_sync


class HostPowerPolicyTests(unittest.TestCase):
    def eligible_kiosk(self, **overrides):
        facts = {
            "mode": "kiosk",
            "install_phase": "installed",
            "kiosk_session_exclusive": True,
            "active_vm_ids": ["vm-a"],
            "current_vm_id": "vm-a",
            "vm_state_known": True,
            "active_job_count": 0,
            "job_state_known": True,
            "active_storage_tasks": 0,
            "storage_state_known": True,
        }
        facts.update(overrides)
        return evaluate_host_power_sync(**facts)

    def test_default_is_vm_only_and_never_authorizes_host_action(self):
        decision = evaluate_host_power_sync(
            install_phase="installed",
            kiosk_session_exclusive=True,
            active_vm_ids=["vm-a"],
            current_vm_id="vm-a",
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "vm-only")

    def test_kiosk_requires_explicit_exclusive_installed_session(self):
        for kwargs, reason in (
            ({"install_phase": "installing", "kiosk_session_exclusive": True}, "installation-not-confirmed"),
            ({"install_phase": "installed", "kiosk_session_exclusive": False}, "kiosk-session-not-exclusive"),
        ):
            with self.subTest(kwargs=kwargs):
                decision = self.eligible_kiosk(**kwargs)
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.reason, reason)

    def test_second_active_vm_blocks_host_power_sync(self):
        decision = self.eligible_kiosk(
            active_vm_ids=["vm-a", "vm-b"],
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "another-vm-active")

    def test_active_or_unknown_storage_work_blocks_host_power_sync(self):
        for count, reason in ((1, "storage-task-active"), (-1, "storage-task-state-unknown"), ("unknown", "storage-task-state-unknown")):
            with self.subTest(count=count):
                decision = self.eligible_kiosk(
                    active_storage_tasks=count,
                )
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.reason, reason)

    def test_only_explicit_kiosk_with_no_other_resources_is_eligible(self):
        decision = self.eligible_kiosk()
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "exclusive-installed-kiosk")

    def test_unknown_vm_job_or_storage_snapshots_fail_closed(self):
        for overrides, reason in (
            ({"vm_state_known": False}, "vm-state-unknown"),
            ({"job_state_known": False}, "job-state-unknown"),
            ({"storage_state_known": False}, "storage-task-state-unknown"),
        ):
            with self.subTest(overrides=overrides):
                decision = self.eligible_kiosk(**overrides)
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.reason, reason)

    def test_current_vm_must_be_present_in_confirmed_active_snapshot(self):
        decision = self.eligible_kiosk(active_vm_ids=[])
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "current-vm-not-active")

    def test_any_other_active_job_blocks_host_power_sync(self):
        decision = self.eligible_kiosk(active_job_count=1)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "job-active")

if __name__ == "__main__":
    unittest.main()
