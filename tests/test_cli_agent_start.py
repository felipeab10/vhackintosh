from __future__ import annotations

import importlib
import unittest
from unittest.mock import Mock, patch

from core.config import VMConfig
from core.web_client import WebAgentUnavailable


class CliAgentStartTests(unittest.TestCase):
    def setUp(self):
        self.cli = importlib.import_module("cli.main")
        self.stale_vm = VMConfig(id="old-id", name="Mac Studio", macos_version="ventura")
        self.remote_vm = VMConfig(id="new-id", name="Mac Studio", macos_version="tahoe")

    def _install_cli_fakes(self):
        store = Mock()
        store.list_vms.return_value = [self.stale_vm]
        tui = Mock()
        return store, tui

    def test_appliance_start_resolves_vm_from_agent_catalog_not_stale_local_store(self):
        store, tui = self._install_cli_fakes()
        remote = {"revision": "r1", "items": [{
            "config": self.remote_vm.to_dict(), "revision": "r1",
        }]}
        with patch.object(self.cli, "VMManagerStore", return_value=store), \
             patch.object(self.cli, "VMTUI", return_value=tui), \
             patch.object(self.cli, "agent_is_required", return_value=True), \
             patch("core.web_client.call_agent", return_value=remote) as rpc, \
             patch.object(self.cli.sys, "argv", ["vhackintosh", "start", "Mac Studio"]):
            self.cli.main()

        rpc.assert_called_once_with("vms.list")
        store.list_vms.assert_not_called()
        tui._launch_vm.assert_called_once()
        self.assertEqual(tui._launch_vm.call_args.args[0].id, "new-id")

    def test_appliance_start_stops_on_rpc_failure_without_local_catalog_fallback(self):
        store, tui = self._install_cli_fakes()
        with patch.object(self.cli, "VMManagerStore", return_value=store), \
             patch.object(self.cli, "VMTUI", return_value=tui), \
             patch.object(self.cli, "agent_is_required", return_value=True), \
             patch("core.web_client.call_agent", side_effect=WebAgentUnavailable("offline")), \
             patch.object(self.cli.sys, "argv", ["vhackintosh", "start", "Mac Studio"]):
            self.cli.main()

        store.list_vms.assert_not_called()
        tui._launch_vm.assert_not_called()

    def test_appliance_list_renders_catalog_received_from_agent(self):
        store, tui = self._install_cli_fakes()
        remote = {"revision": "r1", "items": [{
            "config": {**self.remote_vm.to_dict(), "revision": "r1"},
            "revision": "r1",
        }]}
        with patch.object(self.cli, "VMManagerStore", return_value=store), \
             patch.object(self.cli, "VMTUI", return_value=tui), \
             patch.object(self.cli, "agent_is_required", return_value=True), \
             patch("core.web_client.call_agent", return_value=remote), \
             patch.object(self.cli.sys, "argv", ["vhackintosh", "list"]):
            self.cli.main()

        store.list_vms.assert_not_called()
        tui._render_vm_table.assert_called_once()
        self.assertEqual(tui._render_vm_table.call_args.args[0][0].id, "new-id")


if __name__ == "__main__":
    unittest.main()
