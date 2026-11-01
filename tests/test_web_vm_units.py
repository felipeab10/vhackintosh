from __future__ import annotations

import subprocess
import unittest
from unittest.mock import Mock, call

import psutil

from core.web_vm_units import (
    InvalidVmUnitId,
    SystemdVmUnitManager,
    VmUnitError,
)


def show_result(
    *,
    load_state: str = "loaded",
    active_state: str = "active",
    sub_state: str = "running",
    main_pid: int = 4321,
    result: str = "success",
) -> subprocess.CompletedProcess[str]:
    output = "\n".join((
        f"LoadState={load_state}",
        f"ActiveState={active_state}",
        f"SubState={sub_state}",
        f"MainPID={main_pid}",
        f"Result={result}",
    )) + "\n"
    return subprocess.CompletedProcess(["systemctl", "show"], 0, output, "")


class SystemdVmUnitManagerTests(unittest.TestCase):
    def make_manager(self, responses=(), *, process_factory=None, runner=None, scope="system"):
        if runner is None:
            runner = Mock(side_effect=list(responses))
        if process_factory is None:
            process_factory = lambda _pid: Mock(create_time=lambda: 1234.5)
        return SystemdVmUnitManager(
            systemctl="/usr/bin/systemctl",
            scope=scope,
            timeout=2.5,
            run=runner,
            process_factory=process_factory,
        ), runner

    def test_rejects_invalid_ids_before_invoking_systemctl(self):
        manager, runner = self.make_manager()
        for vm_id in ("", "../vm", "VM-A", "vm_a", "vm.a", "-vm", "vm-", "a" * 64, None):
            with self.subTest(vm_id=vm_id):
                with self.assertRaises(InvalidVmUnitId):
                    manager.query(vm_id)
        runner.assert_not_called()

    def test_query_parses_active_inactive_and_failed_states(self):
        responses = [
            show_result(),
            show_result(active_state="inactive", sub_state="dead", main_pid=0),
            show_result(active_state="failed", sub_state="failed", main_pid=0, result="exit-code"),
        ]
        manager, _runner = self.make_manager(responses)

        active = manager.query("vm-a")
        inactive = manager.query("vm-b")
        failed = manager.query("vm-c")

        self.assertEqual((active.active_state, active.sub_state), ("active", "running"))
        self.assertEqual(active.unit, "vhackintosh-vm@vm-a.service")
        self.assertEqual((inactive.active_state, inactive.main_pid), ("inactive", None))
        self.assertEqual((failed.active_state, failed.result), ("failed", "exit-code"))

    def test_query_reads_main_pid_and_create_time_when_available(self):
        process_factory = Mock(return_value=Mock(create_time=Mock(return_value=456.75)))
        manager, _runner = self.make_manager([show_result(main_pid=98)], process_factory=process_factory)

        status = manager.query("macos-studio")

        self.assertEqual(status.main_pid, 98)
        self.assertEqual(status.process_create_time, 456.75)
        process_factory.assert_called_once_with(98)

    def test_query_keeps_timestamp_unavailable_when_process_disappears(self):
        process_factory = Mock(side_effect=psutil.NoSuchProcess(98))
        manager, _runner = self.make_manager([show_result(main_pid=98)], process_factory=process_factory)

        status = manager.query("macos-studio")

        self.assertEqual(status.main_pid, 98)
        self.assertIsNone(status.process_create_time)

    def test_query_rejects_incomplete_or_malformed_show_output(self):
        manager, _runner = self.make_manager([
            subprocess.CompletedProcess(["systemctl"], 0, "ActiveState=active\n", ""),
            subprocess.CompletedProcess(["systemctl"], 0, "ActiveState=active\nActiveState=failed\n", ""),
            show_result(active_state="surprise"),
            show_result(main_pid=-1),
            show_result(main_pid="not-a-pid"),
        ])
        for _ in range(5):
            with self.assertRaises(VmUnitError):
                manager.query("vm-a")

    def test_systemctl_timeout_and_nonzero_exit_are_reported(self):
        runner = Mock(side_effect=[
            subprocess.TimeoutExpired(["systemctl", "show"], 2.5),
            subprocess.CompletedProcess(["systemctl", "show"], 1, "", "unit unavailable"),
        ])
        manager, _runner = self.make_manager(runner=runner)

        with self.assertRaisesRegex(VmUnitError, "timed out"):
            manager.query("vm-a")
        with self.assertRaisesRegex(VmUnitError, "unit unavailable"):
            manager.query("vm-a")

    def test_start_and_stop_use_only_validated_unit_id(self):
        runner = Mock(side_effect=[
            subprocess.CompletedProcess(["systemctl", "start"], 0, "", ""),
            show_result(),
            subprocess.CompletedProcess(["systemctl", "stop"], 0, "", ""),
            show_result(active_state="inactive", sub_state="dead", main_pid=0),
        ])
        manager, _runner = self.make_manager(runner=runner)

        self.assertEqual(manager.start("vm-a").active_state, "active")
        self.assertEqual(manager.stop("vm-a").active_state, "inactive")
        self.assertEqual(
            [invocation.args[0] for invocation in runner.call_args_list],
            [
                ["/usr/bin/systemctl", "start", "vhackintosh-vm@vm-a.service"],
                ["/usr/bin/systemctl", "show", "--no-pager",
                 "--property=LoadState", "--property=ActiveState", "--property=SubState",
                 "--property=MainPID", "--property=Result", "vhackintosh-vm@vm-a.service"],
                ["/usr/bin/systemctl", "stop", "vhackintosh-vm@vm-a.service"],
                ["/usr/bin/systemctl", "show", "--no-pager",
                 "--property=LoadState", "--property=ActiveState", "--property=SubState",
                 "--property=MainPID", "--property=Result", "vhackintosh-vm@vm-a.service"],
            ],
        )

    def test_admission_failure_message_reads_only_allowlisted_unit_summary(self):
        runner = Mock(return_value=subprocess.CompletedProcess(
            ["journalctl"],
            0,
            "unrelated host log\n"
            "vm-supervisor: VM vm-a rejected: insufficient_host_memory: "
            "Memória insuficiente: solicita 8 GiB, disponíveis 2 GiB.\n",
            "",
        ))
        manager, _runner = self.make_manager(runner=runner)

        message = manager.admission_failure_message("vm-a")

        self.assertEqual(message, "Memória insuficiente: solicita 8 GiB, disponíveis 2 GiB.")
        self.assertEqual(
            runner.call_args.args[0],
            [
                "/usr/bin/journalctl",
                "--unit=vhackintosh-vm@vm-a.service",
                "--no-pager",
                "--output=cat",
                "--lines=50",
            ],
        )

    def test_admission_failure_message_hides_non_allowlisted_diagnostics(self):
        runner = Mock(return_value=subprocess.CompletedProcess(
            ["journalctl"],
            0,
            "vm-supervisor: VM vm-a rejected: runtime_unavailable: "
            "sensitive path /home/user/private\n",
            "",
        ))
        manager, _runner = self.make_manager(runner=runner)

        self.assertIsNone(manager.admission_failure_message("vm-a"))

    def test_admission_failure_message_uses_user_journal_for_user_units(self):
        runner = Mock(return_value=subprocess.CompletedProcess(
            ["journalctl"],
            0,
            "vm-supervisor: VM vm-a rejected: reservation_storage_full: "
            "Não há espaço para a reserva de recursos.\n",
            "",
        ))
        manager, _runner = self.make_manager(runner=runner, scope="user")

        message = manager.admission_failure_message("vm-a")

        self.assertEqual(message, "Não há espaço para a reserva de recursos.")
        self.assertEqual(
            runner.call_args.args[0],
            [
                "/usr/bin/journalctl",
                "--user",
                "--unit=vhackintosh-vm@vm-a.service",
                "--no-pager",
                "--output=cat",
                "--lines=50",
            ],
        )

    def test_start_and_stop_reject_invalid_id_without_command(self):
        manager, runner = self.make_manager()
        with self.assertRaises(InvalidVmUnitId):
            manager.start("../vm")
        with self.assertRaises(InvalidVmUnitId):
            manager.stop("../vm")
        runner.assert_not_called()

    def test_user_scope_targets_only_the_user_manager(self):
        manager, runner = self.make_manager([show_result()], scope="user")

        manager.query("vm-a")

        self.assertEqual(runner.call_args.args[0][:3], ["/usr/bin/systemctl", "--user", "show"])

    def test_rejects_unknown_systemd_scope(self):
        with self.assertRaisesRegex(ValueError, "systemd scope"):
            SystemdVmUnitManager(scope="global")


if __name__ == "__main__":
    unittest.main()
