from __future__ import annotations

import os
import unittest
from unittest.mock import Mock, patch

from core.vm_lifecycle import LaunchResult
from core.vm_supervisor import run_vm


class VmSupervisorEntrypointTests(unittest.TestCase):
    def test_rejects_unsafe_instance_before_reading_catalog(self):
        store_factory = Mock(side_effect=AssertionError("catalog must not be opened"))
        for vm_id in ("../outside", "invalid-", "a" * 64):
            with self.subTest(vm_id=vm_id), self.assertRaisesRegex(ValueError, "invalid VM ID"):
                run_vm(vm_id, store_factory=store_factory)
        store_factory.assert_not_called()

    def test_unknown_vm_exits_without_starting_lifecycle(self):
        store = Mock()
        store.get_vm.return_value = None
        service_factory = Mock(side_effect=AssertionError("lifecycle must not start"))

        self.assertEqual(run_vm("macos-test", store_factory=lambda _path: store, lifecycle_factory=service_factory), 2)
        service_factory.assert_not_called()

    def test_runs_existing_lifecycle_pipeline_for_catalog_vm(self):
        vm = Mock()
        store = Mock()
        store.get_vm.return_value = vm
        lifecycle = Mock()
        lifecycle.start.return_value = LaunchResult(vm_id="macos-test", state="completed", phase="installed")

        result = run_vm("macos-test", store_factory=lambda _path: store, lifecycle_factory=lambda _store: lifecycle)

        self.assertEqual(result, 0)
        lifecycle.start.assert_called_once_with(vm)

    def test_desktop_mode_uses_the_active_users_graphical_session(self):
        vm = Mock()
        store = Mock()
        store.get_vm.return_value = vm
        lifecycle = Mock()
        lifecycle.start.return_value = LaunchResult(
            vm_id="macos-test", state="completed", phase="installed"
        )
        display = {
            "XDG_RUNTIME_DIR": "/run/user/1000",
            "WAYLAND_DISPLAY": "wayland-1",
            "DISPLAY": ":0",
            "XDG_SESSION_TYPE": "wayland",
        }

        with (
            patch.dict(os.environ, {"VHACKINTOSH_VM_DISPLAY_MODE": "desktop"}),
            patch("core.vm_supervisor.discover_graphical_environment", return_value=display),
        ):
            result = run_vm(
                "macos-test",
                store_factory=lambda _path: store,
                lifecycle_factory=lambda _store: lifecycle,
            )
            self.assertEqual(os.environ["WAYLAND_DISPLAY"], "wayland-1")

        self.assertEqual(result, 0)
        lifecycle.start.assert_called_once_with(vm)

    def test_failed_lifecycle_returns_failure_without_retry(self):
        vm = Mock()
        store = Mock()
        store.get_vm.return_value = vm
        lifecycle = Mock()
        lifecycle.start.return_value = LaunchResult(
            vm_id="macos-test", state="rejected", phase="pending",
            error_code="runtime_unavailable", message="release missing",
        )

        self.assertEqual(run_vm("macos-test", store_factory=lambda _path: store, lifecycle_factory=lambda _store: lifecycle), 1)
        lifecycle.start.assert_called_once_with(vm)


if __name__ == "__main__":
    unittest.main()
