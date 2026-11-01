"""Regressões das regras de preparação de lançamento sem dependência da TUI."""

from __future__ import annotations

import datetime
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from core.config import ExclusiveVMLock, VMConfig, VMManagerStore
from core.install_phase import DiskProbe, InstallEvidence, InstallPhase
from core.vm_lifecycle import (
    LaunchHooks,
    VMLifecycleService,
    assess_post_install,
    default_launch_hooks,
    prepare_install_launch,
    record_install_launch,
    run_rail_session,
)


def make_vm(**changes):
    values = {
        "id": "macos-test",
        "name": "macOS Test",
        "macos_version": "tahoe",
        "disk_path": "/tmp/macos-test.qcow2",
    }
    values.update(changes)
    return VMConfig(**values)


class InstallLaunchPreparationTests(unittest.TestCase):
    def test_forced_installation_attaches_first_existing_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            custom = root / "vm"
            config = root / "config"
            home = root / "home"
            candidates = [
                custom / "BaseSystem.img",
                config / "images" / "tahoe" / "BaseSystem.img",
                home / ".config" / "vhackintosh" / "images" / "tahoe" / "BaseSystem.img",
            ]
            for candidate in candidates:
                candidate.parent.mkdir(parents=True, exist_ok=True)
            candidates[1].touch()
            candidates[2].touch()
            vm = make_vm(install_phase=InstallPhase.INSTALLED.value)
            probe = DiskProbe(exists=True, disk_gb=20, has_apfs=True)

            prepared = prepare_install_launch(
                vm,
                force_installation=True,
                custom_dir=custom,
                config_dir=config,
                home=home,
                probe=probe,
                now=datetime.datetime(2026, 10, 29, 12, 30, 5),
            )

            self.assertEqual(prepared.policy.phase, InstallPhase.INSTALLING)
            self.assertTrue(prepared.policy.attach_installer)
            self.assertFalse(prepared.policy.enable_host_power_sync)
            self.assertEqual(prepared.installer_image, str(candidates[1]))
            self.assertEqual(prepared.evidence, InstallEvidence.COMPLETE)
            self.assertEqual(prepared.booted_at, "2026-10-29T12:30:05")
            record_install_launch(vm, prepared)
            self.assertEqual(vm.install_phase, "installing")
            self.assertEqual(vm.install_evidence, "complete")
            self.assertEqual(vm.last_booted_at, "2026-10-29T12:30:05")

    def test_installed_phase_detaches_installer_and_keeps_host_sync(self):
        with tempfile.TemporaryDirectory() as tmp:
            custom = Path(tmp)
            (custom / "BaseSystem.img").touch()
            vm = make_vm(install_phase=InstallPhase.INSTALLED.value)
            prepared = prepare_install_launch(
                vm,
                custom_dir=custom,
                probe=DiskProbe(exists=True, disk_gb=20, has_apfs=True),
            )
        self.assertEqual(prepared.policy.phase, InstallPhase.INSTALLED)
        self.assertFalse(prepared.policy.attach_installer)
        self.assertTrue(prepared.policy.enable_host_power_sync)
        self.assertIsNone(prepared.installer_image)

    def test_partial_disk_evidence_remains_installing_and_never_syncs_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            vm = make_vm(install_phase=InstallPhase.INSTALLING.value)
            prepared = prepare_install_launch(
                vm,
                home=Path(tmp),
                config_dir=Path(tmp) / "config",
                probe=DiskProbe(exists=True, disk_gb=5, has_apfs=True),
            )
        self.assertEqual(prepared.policy.phase, InstallPhase.INSTALLING)
        self.assertTrue(prepared.policy.attach_installer)
        self.assertFalse(prepared.policy.enable_host_power_sync)
        self.assertEqual(prepared.evidence, InstallEvidence.PARTIAL)
        self.assertIsNone(prepared.installer_image)


class PostInstallAssessmentTests(unittest.TestCase):
    def test_complete_evidence_is_assessed_but_never_promotes_vm_automatically(self):
        vm = make_vm(install_phase=InstallPhase.INSTALLING.value)
        assessment = assess_post_install(
            vm, DiskProbe(exists=True, disk_gb=20, has_apfs=True)
        )
        self.assertTrue(assessment.looks_complete)
        self.assertEqual(assessment.evidence, InstallEvidence.COMPLETE)
        self.assertEqual(vm.install_phase, InstallPhase.INSTALLING.value)

    def test_incomplete_evidence_is_reported(self):
        assessment = assess_post_install(make_vm(), DiskProbe(exists=False))
        self.assertFalse(assessment.looks_complete)
        self.assertEqual(assessment.evidence, InstallEvidence.NONE)


class RailSessionTests(unittest.TestCase):
    def test_failed_start_result_with_install_reset_still_relaunches(self):
        monitors = iter([
            mock.Mock(reset_count=1, last_reason="guest-reset"),
            mock.Mock(reset_count=0, last_reason=None),
        ])
        sessions = iter([17, 0])
        launches = []

        def make_monitor():
            monitor = next(monitors)
            monitor.wait_for_disconnect = mock.Mock()
            return monitor

        def launch():
            launches.append(1)
            return mock.Mock(wait=mock.Mock(return_value=next(sessions)))

        resets, reason = run_rail_session(
            launch, make_monitor, is_installing=True, settle_sec=0
        )

        self.assertEqual(len(launches), 2)
        self.assertEqual((resets, reason), (1, "guest-reset"))

    def test_default_hooks_keep_supervised_qemu_alive_until_process_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            store = VMManagerStore(Path(directory) / "vms.json")
            vm = make_vm()
            hooks = default_launch_hooks(store)

            supervised = hooks.session_factory(
                ["reims-ctl", "start", vm.id],
                {"VHACKINTOSH_VM_SUPERVISOR": "1"},
                vm,
                lambda _vm: None,
            )
            interactive = hooks.session_factory(
                ["reims-ctl", "start", vm.id], {}, vm, lambda _vm: None
            )

        self.assertIsNone(supervised.wait_timeout_sec)
        self.assertGreater(interactive.wait_timeout_sec, 0)

    def test_reboot_relaunches_only_during_installation_and_aggregates_resets(self):
        monitors = iter([
            mock.Mock(reset_count=1, last_reason="guest-reset"),
            mock.Mock(reset_count=0, last_reason=None),
        ])
        launches = []

        def make_monitor():
            monitor = next(monitors)
            monitor.wait_for_disconnect = mock.Mock()
            return monitor

        def launch():
            launches.append(1)
            return mock.Mock(wait=mock.Mock())

        notices = []
        resets, reason = run_rail_session(
            launch, make_monitor, is_installing=True, settle_sec=0, on_relaunch=notices.append
        )

        self.assertEqual(len(launches), 2)
        self.assertEqual((resets, reason), (1, "guest-reset"))
        self.assertEqual(notices, [1])

    def test_installed_vm_does_not_relaunch_even_after_guest_reset(self):
        monitor = mock.Mock(reset_count=2, last_reason="guest-reset")
        monitor.wait_for_disconnect = mock.Mock()
        launch = mock.Mock(return_value=mock.Mock(wait=mock.Mock()))
        result = run_rail_session(
            launch, lambda: monitor, is_installing=False, settle_sec=0
        )
        self.assertEqual(launch.call_count, 1)
        self.assertEqual(result, (2, "guest-reset"))

    def test_relaunch_cap_notifies_and_stops_after_limit(self):
        monitor = mock.Mock(reset_count=1, last_reason="guest-reset")
        monitor.wait_for_disconnect = mock.Mock()
        launch = mock.Mock(return_value=mock.Mock(wait=mock.Mock()))
        notices = []
        result = run_rail_session(
            launch,
            lambda: monitor,
            is_installing=True,
            max_relaunches=1,
            settle_sec=0,
            on_relaunch=notices.append,
        )
        self.assertEqual(launch.call_count, 2)
        self.assertEqual(result, (2, "guest-reset"))
        self.assertEqual(notices, [1, -1])


class SharedLifecycleServiceTests(unittest.TestCase):
    def test_admission_rejection_explains_insufficient_host_memory(self):
        class Lock:
            last_error = {
                "requested_gb": 8,
                "available_gb": 2.5,
                "active_vms": ["macOS A"],
            }

            def acquire(self, *_args, **_kwargs):
                return False

            def release(self):
                raise AssertionError("unacquired reservation must not be released")

        with tempfile.TemporaryDirectory() as directory:
            store = VMManagerStore(Path(directory) / "vms.json")
            service = VMLifecycleService(
                store,
                hooks=default_launch_hooks(store),
                lock_factory=Lock,
            )
            result = service.start(make_vm())

        self.assertEqual(result.error_code, "insufficient_host_memory")
        self.assertIn("solicita 8 GiB", result.message)
        self.assertIn("2.5 GiB disponíveis", result.message)
        self.assertEqual(result.details["active_vms"], ["macOS A"])

    def test_admission_rejection_explains_reservation_storage_full(self):
        class Lock:
            last_error = {"reason": "storage_full", "resource": "vm_reservation"}

            def acquire(self, *_args, **_kwargs):
                return False

            def release(self):
                raise AssertionError("unacquired reservation must not be released")

        with tempfile.TemporaryDirectory() as directory:
            store = VMManagerStore(Path(directory) / "vms.json")
            service = VMLifecycleService(
                store,
                hooks=default_launch_hooks(store),
                lock_factory=Lock,
            )
            result = service.start(make_vm())

        self.assertEqual(result.error_code, "reservation_storage_full")
        self.assertIn("armazenamento temporário", result.message)

    def test_admission_rejection_explains_occupied_loopback_ssh_port(self):
        class Lock:
            last_error = {"reason": "network_port_in_use", "network_ports": [2222]}

            def acquire(self, *_args, **_kwargs):
                return False

            def release(self):
                raise AssertionError("unacquired reservation must not be released")

        with tempfile.TemporaryDirectory() as directory:
            store = VMManagerStore(Path(directory) / "vms.json")
            service = VMLifecycleService(
                store, hooks=default_launch_hooks(store), lock_factory=Lock,
            )
            result = service.start(make_vm(network_ssh_port=2222))

        self.assertEqual(result.error_code, "network_port_in_use")
        self.assertIn("porta local 2222", result.message)
        self.assertIn("Escolha outra porta", result.message)

    def test_two_distinct_vms_can_start_concurrently(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = VMManagerStore(root / "vms.json")
            vms = [
                make_vm(id="macos-a", name="macOS A", disk_path=str(root / "a.qcow2")),
                make_vm(id="macos-b", name="macOS B", disk_path=str(root / "b.qcow2")),
            ]
            for vm in vms:
                store.add_vm(vm)
            entered = {vm.id: threading.Event() for vm in vms}
            release = threading.Event()
            launches = []

            class Monitor:
                reset_count = 0
                last_reason = None

                def start(self):
                    pass

                def stop(self):
                    pass

                def wait_for_disconnect(self, timeout_sec):
                    pass

            def session_factory(_argv, _env, vm, _usb_callback):
                launches.append(vm.id)

                def wait():
                    entered[vm.id].set()
                    if not release.wait(timeout=3):
                        raise TimeoutError("test did not release both VM launches")
                    return 0

                return SimpleNamespace(wait=wait)

            hooks = LaunchHooks(
                find_reims_dir=lambda: None,
                vulkan_readiness=lambda: (True, "ready"),
                ensure_release=lambda _path, _vm_id: True,
                recover_storage=lambda _vm: (None, ""),
                decide_runtime=lambda *_args: (True, "ready"),
                apply_graphics_environment=lambda _env: None,
                on_usb_ready=lambda _vm: None,
                apply_gpu_environment=lambda _vm, _env: None,
                write_vm_config=lambda _vm, **_kwargs: None,
                ensure_registered=lambda _vm: None,
                ctl_binary=lambda: "/usr/bin/reims-ctl",
                session_argv=lambda argv, _env: (argv, False),
                monitor_factory=lambda _vm: Monitor(),
                session_factory=session_factory,
                apply_power_policy=lambda *_args: None,
                post_phase_update=lambda *_args: None,
                event=lambda *_args: None,
            )
            patches = (
                mock.patch("core.config.VM_RESERVATIONS_DIR", root / "reservations"),
                mock.patch("core.config.VM_RESERVATIONS_LOCK", root / "reservations.lock"),
                mock.patch("core.config.VM_MAINTENANCE_MARKER", root / "maintenance.json"),
                mock.patch.object(ExclusiveVMLock, "_running_reims_vms", return_value=({}, True)),
                mock.patch.object(ExclusiveVMLock, "_host_memory", return_value=(64 * 1024**3, 63 * 1024**3)),
                mock.patch.object(ExclusiveVMLock, "_process_rss", return_value=0),
                mock.patch.object(ExclusiveVMLock, "_registered_vm_resources", return_value={}),
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
                service = VMLifecycleService(store, hooks=hooks)
                with ThreadPoolExecutor(max_workers=2) as executor:
                    futures = [executor.submit(service.start, vm) for vm in vms]
                    try:
                        self.assertTrue(entered["macos-a"].wait(timeout=2))
                        self.assertTrue(entered["macos-b"].wait(timeout=2))
                        self.assertEqual(len(ExclusiveVMLock._read_reservations()), 2)
                        self.assertCountEqual(launches, ["macos-a", "macos-b"])
                    finally:
                        release.set()
                    results = [future.result(timeout=4) for future in futures]

                self.assertEqual([result.state for result in results], ["completed", "completed"])
                self.assertEqual(ExclusiveVMLock._read_reservations(), [])

    def test_service_owns_ordered_reims_launch_and_all_runtime_callbacks(self):
        order = []

        class Store:
            def update_vm(self, vm):
                order.append(("save", vm.install_phase))

        class Lock:
            last_error = None

            def acquire(self, *_args, **_kwargs):
                order.append(("reserve",))
                return True

            def release(self):
                order.append(("release",))

        class Monitor:
            reset_count = 0
            last_reason = None

            def start(self):
                order.append(("monitor.start",))

            def stop(self):
                order.append(("monitor.stop",))

        def wait():
            order.append(("session.wait",))
            callback()

        vm = make_vm(disk_path="", install_phase=InstallPhase.INSTALLED.value)
        hooks = LaunchHooks(
            find_reims_dir=lambda: Path("/reims"),
            vulkan_readiness=lambda: (True, "ready"),
            ensure_release=lambda path, vm_id: order.append(("release.ensure", path, vm_id)) or True,
            recover_storage=lambda item: (None, ""),
            decide_runtime=lambda *_args: (True, "reims-ctl ready"),
            apply_graphics_environment=lambda _env: order.append(("graphics",)),
            on_usb_ready=lambda item: order.append(("usb", item.id)),
            apply_gpu_environment=lambda _vm, _env: order.append(("gpu",)),
            write_vm_config=lambda item, **kwargs: order.append(("toml", kwargs.get("installer"))),
            ensure_registered=lambda item: order.append(("register", item.id)),
            ctl_binary=lambda: "/fake/reims-ctl",
            session_argv=lambda argv, _env: (order.append(("kiosk",)) or argv, False),
            monitor_factory=lambda item: Monitor(),
            session_factory=lambda argv, env, item, ready: (
                order.append(("session.create", argv[1], item.id))
                or set_callback(ready, item)
            ),
            apply_power_policy=lambda item, _policy, _monitor: order.append(("power", item.id)),
            post_phase_update=lambda item, _policy: order.append(("post", item.id)),
            event=lambda name, _payload: order.append(("event", name)),
        )

        def set_callback(ready, item):
            nonlocal callback
            callback = lambda: ready(item)
            return type("Session", (), {"wait": staticmethod(wait)})()

        callback = lambda: None
        service = VMLifecycleService(Store(), lock=Lock(), hooks=hooks)
        result = service.start(vm)

        self.assertEqual(result.state, "completed")
        self.assertEqual(result.phase, "installed")
        self.assertIn(("release.ensure", "/reims", vm.id), order)
        self.assertLess(order.index(("toml", None)), order.index(("register", vm.id)))
        self.assertLess(order.index(("register", vm.id)), order.index(("session.create", "start", vm.id)))
        self.assertLess(order.index(("monitor.start",)), order.index(("session.wait",)))
        self.assertLess(order.index(("usb", vm.id)), order.index(("power", vm.id)))
        self.assertLess(order.index(("power", vm.id)), order.index(("post", vm.id)))
        self.assertEqual(order[-1], ("release",))

    def test_tui_delegates_launch_to_the_shared_lifecycle_service(self):
        from ui.tui import VMTUI

        vm = make_vm()
        result = mock.Mock(state="completed")
        service = mock.Mock(start=mock.Mock(return_value=result))
        tui = object.__new__(VMTUI)
        tui.lifecycle_service = service
        with mock.patch("ui.tui.console.clear"), \
             mock.patch("ui.tui.console.print"), \
             mock.patch("ui.tui.centered_banner", return_value="banner"), \
             mock.patch("ui.tui.sys.stdin.isatty", return_value=False):
            tui._launch_vm(vm, is_installation=True)
        service.start.assert_called_once()
        args, kwargs = service.start.call_args
        self.assertIs(args[0], vm)
        self.assertTrue(kwargs["force_installation"])
        self.assertTrue(callable(kwargs["on_event"]))

    def test_partial_reims_launch_releases_reservation_and_retry_adopts_live_vm(self):
        """A failed start with a live QEMU is rejected and cannot be duplicated."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reservations = root / "reservations"
            running = {}
            launch_attempts = []

            class Monitor:
                reset_count = 0
                last_reason = None

                def start(self):
                    pass

                def stop(self):
                    pass

            def partial_launch(*_args):
                launch_attempts.append("start")
                # Model Reims having forked QEMU and created its QMP endpoint,
                # then failing before its start handshake returns successfully.
                running["macos-test"] = 4242
                (root / "qmp.sock").touch()
                return SimpleNamespace(wait=lambda: 17)

            hooks = LaunchHooks(
                find_reims_dir=lambda: Path("/reims"),
                vulkan_readiness=lambda: (True, "ready"),
                ensure_release=lambda _path, _vm_id: True,
                recover_storage=lambda _vm: (None, ""),
                decide_runtime=lambda *_args: (True, "ready"),
                apply_graphics_environment=lambda _env: None,
                on_usb_ready=lambda _vm: None,
                apply_gpu_environment=lambda _vm, _env: None,
                write_vm_config=lambda _vm, **_kwargs: None,
                ensure_registered=lambda _vm: None,
                ctl_binary=lambda: "/usr/bin/reims-ctl",
                session_argv=lambda argv, _env: (argv, False),
                monitor_factory=lambda _vm: Monitor(),
                session_factory=partial_launch,
                apply_power_policy=lambda *_args: None,
                post_phase_update=lambda *_args: None,
                event=lambda *_args: None,
            )
            patches = (
                mock.patch("core.config.VM_RESERVATIONS_DIR", reservations),
                mock.patch("core.config.VM_RESERVATIONS_LOCK", root / "reservations.lock"),
                mock.patch("core.config.VM_MAINTENANCE_MARKER", root / "maintenance.json"),
                mock.patch.object(
                    ExclusiveVMLock, "_running_reims_vms",
                    side_effect=lambda: (dict(running), True),
                ),
                mock.patch.object(
                    ExclusiveVMLock, "_host_memory",
                    return_value=(64 * 1024**3, 63 * 1024**3),
                ),
                mock.patch.object(ExclusiveVMLock, "_process_rss", return_value=0),
                mock.patch.object(ExclusiveVMLock, "_registered_vm_resources", return_value={}),
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
                service = VMLifecycleService(
                    VMManagerStore(root / "vms.json"), hooks=hooks
                )
                vm = make_vm(disk_path="", install_phase=InstallPhase.INSTALLED.value)

                failed = service.start(vm)

                # The Python lifecycle drops its reservation in `finally` and
                # returns failure even when the guest remains visible in Reims.
                self.assertEqual(failed.state, "rejected")
                self.assertEqual(failed.error_code, "launch_failed")
                self.assertTrue(failed.details["runtime_state_known"])
                self.assertTrue(failed.details["process_still_running"])
                self.assertEqual(ExclusiveVMLock._read_reservations(), [])
                self.assertTrue((root / "qmp.sock").exists())

                # A new admission is allowed to inspect current Reims state,
                # which prevents a second `reims-ctl start` while QEMU lives.
                retry = VMLifecycleService(
                    VMManagerStore(root / "vms.json"), hooks=hooks
                ).start(vm)
                self.assertEqual(retry.state, "rejected")
                self.assertEqual(retry.error_code, "admission_failed")
                self.assertEqual(retry.details.get("reason"), "already_running")
                self.assertEqual(launch_attempts, ["start"])
                self.assertEqual(ExclusiveVMLock._read_reservations(), [])

    def test_failed_reims_start_with_no_live_process_can_be_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            running = {}
            launch_attempts = []

            class Monitor:
                reset_count = 0
                last_reason = None

                def start(self):
                    pass

                def stop(self):
                    pass

            def session_factory(*_args):
                launch_attempts.append("start")
                if len(launch_attempts) == 1:
                    # Reims returned an error but has no surviving guest.
                    return SimpleNamespace(wait=lambda: 17)
                return SimpleNamespace(wait=lambda: 0)

            hooks = LaunchHooks(
                find_reims_dir=lambda: Path("/reims"),
                vulkan_readiness=lambda: (True, "ready"),
                ensure_release=lambda _path, _vm_id: True,
                recover_storage=lambda _vm: (None, ""),
                decide_runtime=lambda *_args: (True, "ready"),
                apply_graphics_environment=lambda _env: None,
                on_usb_ready=lambda _vm: None,
                apply_gpu_environment=lambda _vm, _env: None,
                write_vm_config=lambda _vm, **_kwargs: None,
                ensure_registered=lambda _vm: None,
                ctl_binary=lambda: "/usr/bin/reims-ctl",
                session_argv=lambda argv, _env: (argv, False),
                monitor_factory=lambda _vm: Monitor(),
                session_factory=session_factory,
                apply_power_policy=lambda *_args: None,
                post_phase_update=lambda *_args: None,
                event=lambda *_args: None,
            )
            patches = (
                mock.patch("core.config.VM_RESERVATIONS_DIR", root / "reservations"),
                mock.patch("core.config.VM_RESERVATIONS_LOCK", root / "reservations.lock"),
                mock.patch("core.config.VM_MAINTENANCE_MARKER", root / "maintenance.json"),
                mock.patch.object(
                    ExclusiveVMLock, "_running_reims_vms",
                    side_effect=lambda: (dict(running), True),
                ),
                mock.patch.object(
                    ExclusiveVMLock, "_host_memory",
                    return_value=(64 * 1024**3, 63 * 1024**3),
                ),
                mock.patch.object(ExclusiveVMLock, "_process_rss", return_value=0),
                mock.patch.object(ExclusiveVMLock, "_registered_vm_resources", return_value={}),
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
                vm = make_vm(disk_path="", install_phase=InstallPhase.INSTALLED.value)
                store = VMManagerStore(root / "vms.json")

                failed = VMLifecycleService(store, hooks=hooks).start(vm)
                self.assertEqual(failed.state, "rejected")
                self.assertEqual(failed.error_code, "launch_failed")
                self.assertTrue(failed.details["runtime_state_known"])
                self.assertFalse(failed.details["process_still_running"])
                self.assertEqual(ExclusiveVMLock._read_reservations(), [])

                retried = VMLifecycleService(store, hooks=hooks).start(vm)
                self.assertEqual(retried.state, "completed")
                self.assertEqual(launch_attempts, ["start", "start"])
                self.assertEqual(ExclusiveVMLock._read_reservations(), [])

    def test_api_and_tui_race_for_the_same_vm_creates_only_one_launch(self):
        """Exercise the API's unit-start path against the TUI lifecycle lock."""
        from core.web_agent import WebAgentService

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = VMManagerStore(root / "vms.json")
            vm = make_vm(disk_path="", install_phase=InstallPhase.INSTALLED.value)
            store.add_vm(vm)
            entered_session = threading.Event()
            release_session = threading.Event()
            running = {}

            class Monitor:
                reset_count = 0
                last_reason = None

                def start(self):
                    pass

                def stop(self):
                    pass

                def wait_for_disconnect(self, timeout_sec):
                    pass

            class BlockingSession:
                def wait(self):
                    entered_session.set()
                    if not release_session.wait(timeout=3):
                        raise TimeoutError("test did not release the first launch")
                    return 0

            hooks = LaunchHooks(
                find_reims_dir=lambda: None,
                vulkan_readiness=lambda: (True, "ready"),
                ensure_release=lambda _path, _vm_id: True,
                recover_storage=lambda _vm: (None, ""),
                decide_runtime=lambda *_args: (True, "ready"),
                apply_graphics_environment=lambda _env: None,
                on_usb_ready=lambda _vm: None,
                apply_gpu_environment=lambda _vm, _env: None,
                write_vm_config=lambda _vm, **_kwargs: None,
                ensure_registered=lambda _vm: None,
                ctl_binary=lambda: "/usr/bin/reims-ctl",
                session_argv=lambda argv, _env: (argv, False),
                monitor_factory=lambda _vm: Monitor(),
                session_factory=lambda *_args: BlockingSession(),
                apply_power_policy=lambda *_args: None,
                post_phase_update=lambda *_args: None,
                event=lambda *_args: None,
            )
            lifecycle_factory = lambda _store: VMLifecycleService(store, hooks=hooks)

            class Units:
                def query(self, _vm_id):
                    return SimpleNamespace(
                        unit=f"vhackintosh-vm@{vm.id}.service",
                        active_state="active" if vm.id in running else "inactive",
                    )

                def start(self, vm_id):
                    result = lifecycle_factory(store).start(store.get_vm(vm_id))
                    state = "active" if result.state == "completed" else "inactive"
                    if state == "active":
                        running[vm_id] = 1234
                    return SimpleNamespace(
                        unit=f"vhackintosh-vm@{vm_id}.service", active_state=state
                    )

            patches = [
                mock.patch("core.config.VM_RESERVATIONS_DIR", root / "reservations"),
                mock.patch("core.config.VM_RESERVATIONS_LOCK", root / "reservations.lock"),
                mock.patch("core.config.VM_MAINTENANCE_MARKER", root / "maintenance.json"),
                mock.patch.object(ExclusiveVMLock, "_running_reims_vms", return_value=({}, True)),
                mock.patch.object(ExclusiveVMLock, "_host_memory", return_value=(64 * 1024**3, 63 * 1024**3)),
                mock.patch.object(ExclusiveVMLock, "_process_rss", return_value=0),
                mock.patch.object(ExclusiveVMLock, "_registered_vm_resources", return_value={}),
            ]
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
                agent = WebAgentService(
                    root / "vms.json",
                    jobs_db_path=root / "jobs.sqlite",
                    running_vms=lambda: dict(running),
                    vm_unit_manager=Units(),
                )
                agent.start_jobs()
                try:
                    accepted = agent.vm_action({
                        "vm_id": vm.id,
                        "action": {
                            "operation": "start",
                            "idempotency_key": "api-tui-race-start-0001",
                        },
                    })
                    self.assertTrue(entered_session.wait(timeout=2))

                    # The API's systemd supervisor has reserved and entered the
                    # launch path. The TUI must observe that reservation rather
                    # than racing a second Reims start.
                    tui_result = lifecycle_factory(store).start(store.get_vm(vm.id))
                    self.assertEqual(tui_result.state, "rejected")
                    self.assertEqual(tui_result.error_code, "admission_failed")
                    self.assertEqual(tui_result.details.get("reason"), "already_running")
                    self.assertEqual(len(ExclusiveVMLock._read_reservations()), 1)

                    release_session.set()
                    deadline = time.monotonic() + 2
                    job = None
                    while time.monotonic() < deadline:
                        job = agent.get_job({"job_id": accepted["job_id"]})
                        if job["state"] in {"succeeded", "failed"}:
                            break
                        time.sleep(0.01)
                    self.assertEqual(job["state"], "succeeded")
                    self.assertEqual(job["result"]["state"], "running")
                    self.assertEqual(ExclusiveVMLock._read_reservations(), [])
                finally:
                    release_session.set()
                    agent.close_jobs()


if __name__ == "__main__":
    unittest.main()
