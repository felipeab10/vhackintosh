from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core.config import VMConfig
from core.web_client import WebAgentUnavailable
from ui import tui as tui_module


class AgentRequiredLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.vm = VMConfig(id="vm-exact-id", name="Mac Studio", macos_version="tahoe")
        self.tui = object.__new__(tui_module.VMTUI)
        self.tui.store = Mock()
        self.tui.lifecycle_service = Mock()
        self.tui._catalog_revision = None
        self.tui._agent_vm_snapshots = {}

    def test_launch_refuses_local_lifecycle_when_agent_is_required_and_rpc_is_unavailable(self):
        with patch.object(tui_module, "agent_is_required", return_value=True), \
             patch.object(tui_module, "console"), \
             patch.object(tui_module, "centered_banner", return_value="banner"), \
             patch.object(tui_module, "call_agent", side_effect=WebAgentUnavailable("offline")) as rpc:
            self.tui._launch_vm(self.vm)

        self.tui.lifecycle_service.start.assert_not_called()
        self.assertEqual(rpc.call_args.args[0], "vms.action")
        self.assertEqual(rpc.call_args.args[1]["action"]["operation"], "start")

    def test_capability_unavailable_is_not_fallback_to_lifecycle(self):
        unavailable = WebAgentUnavailable("supervisor unavailable", code="capability_unavailable")
        with patch.object(tui_module, "agent_is_required", return_value=True), \
             patch.object(tui_module, "console"), \
             patch.object(tui_module, "centered_banner", return_value="banner"), \
             patch.object(tui_module, "call_agent", side_effect=unavailable):
            self.tui._launch_vm(self.vm)

        self.tui.lifecycle_service.start.assert_not_called()

    def test_force_stop_sends_exact_vm_id_and_never_calls_reims_ctl(self):
        with patch.object(tui_module, "agent_is_required", return_value=True), \
             patch.object(tui_module, "console"), \
             patch.object(tui_module.Confirm, "ask", return_value=True), \
             patch.object(tui_module.Prompt, "ask", return_value=self.vm.id), \
             patch.object(tui_module, "call_agent", side_effect=WebAgentUnavailable("offline")) as rpc, \
             patch("core.reims_ctl.parar_vm") as local_stop:
            self.tui._parar_vm_interativo(self.vm, 1234, force=True)

        payload = rpc.call_args.args[1]
        self.assertEqual(payload["action"]["operation"], "force_stop")
        self.assertEqual(payload["action"]["confirmation"], self.vm.id)
        local_stop.assert_not_called()

    def test_shutdown_capability_unavailable_never_calls_reims_ctl(self):
        unavailable = WebAgentUnavailable("supervisor unavailable", code="capability_unavailable")
        with patch.object(tui_module, "agent_is_required", return_value=True), \
             patch.object(tui_module, "console"), \
             patch.object(tui_module.Confirm, "ask", return_value=True), \
             patch.object(tui_module, "call_agent", side_effect=unavailable) as rpc, \
             patch("core.reims_ctl.parar_vm") as local_stop:
            self.tui._parar_vm_interativo(self.vm, 1234, force=False)

        self.assertEqual(rpc.call_args.args[1]["action"]["operation"], "shutdown")
        local_stop.assert_not_called()

    def test_appliance_tui_catalog_comes_from_agent(self):
        expected = [self.vm]
        with patch.object(tui_module, "agent_is_required", return_value=True), \
             patch.object(tui_module, "read_agent_catalog", return_value=(expected, "r1")), \
             patch.object(self.tui, "store") as store:
            self.assertEqual(self.tui._list_managed_vms(), expected)
        store.list_vms.assert_not_called()

    def test_agent_unavailable_never_falls_back_to_local_tui_catalog(self):
        unavailable = WebAgentUnavailable("offline")
        with patch.object(tui_module, "agent_is_required", return_value=True), \
             patch.object(tui_module, "read_agent_catalog", side_effect=unavailable), \
             patch.object(self.tui, "store") as store:
            with self.assertRaises(WebAgentUnavailable):
                self.tui._list_managed_vms()
        store.list_vms.assert_not_called()

    def test_catalog_update_uses_agent_revision_and_never_local_store(self):
        self.tui._catalog_revision = "revision-1"
        self.tui._agent_vm_snapshots[self.vm.id] = VMConfig.from_dict(self.vm.to_dict())
        with patch.object(tui_module, "agent_is_required", return_value=True), \
             patch.object(tui_module, "call_agent", return_value={"revision": "revision-2"}) as rpc:
            result = self.tui._update_managed_vm(self.vm)
        self.assertEqual(result["revision"], "revision-2")
        self.assertEqual(rpc.call_args.args[0], "vms.config.update")
        self.assertEqual(rpc.call_args.args[1]["expected_revision"], "revision-1")
        self.tui.store.update_vm.assert_not_called()

    def test_catalog_update_failure_restores_snapshot_without_local_fallback(self):
        snapshot = VMConfig.from_dict(self.vm.to_dict())
        self.tui._catalog_revision = "revision-1"
        self.tui._agent_vm_snapshots[self.vm.id] = snapshot
        self.vm.name = "unsaved rename"
        with patch.object(tui_module, "agent_is_required", return_value=True), \
             patch.object(tui_module, "call_agent", side_effect=WebAgentUnavailable("offline")):
            with self.assertRaises(WebAgentUnavailable):
                self.tui._update_managed_vm(self.vm)
        self.assertEqual(self.vm.name, snapshot.name)
        self.tui.store.update_vm.assert_not_called()

    def test_tui_metadata_patch_uses_agent_and_does_not_write_catalog_locally(self):
        self.tui._catalog_revision = "revision-1"
        self.tui._agent_vm_snapshots[self.vm.id] = VMConfig.from_dict(self.vm.to_dict())
        with patch.object(tui_module, "agent_is_required", return_value=True), \
             patch.object(tui_module, "call_agent", return_value={"revision": "revision-2"}) as rpc:
            self.tui._patch_managed_vm(self.vm, {"usb_auto_attach": ["1050:0407"]})
        self.assertEqual(rpc.call_args.args[0], "vms.config.patch")
        self.assertEqual(rpc.call_args.args[1]["fields"]["usb_auto_attach"], ["1050:0407"])
        self.tui.store.update_vm.assert_not_called()

    def test_delete_config_uses_agent_and_never_local_delete(self):
        self.tui._catalog_revision = "revision-1"
        self.tui._agent_vm_snapshots[self.vm.id] = VMConfig.from_dict(self.vm.to_dict())
        with patch.object(tui_module, "agent_is_required", return_value=True), \
             patch.object(tui_module, "call_agent", return_value={
                 "deleted": True, "revision": "revision-2",
             }) as rpc:
            self.tui._delete_managed_vm(self.vm, delete_disk=False)
        self.assertEqual(rpc.call_args.args[0], "vms.config.delete")
        self.assertEqual(rpc.call_args.args[1]["expected_revision"], "revision-1")
        self.tui.store.delete_vm.assert_not_called()

    def test_tui_provision_uses_agent_job_and_rejects_invalid_acceptance(self):
        request = {
            "idempotency_key": "0123456789abcdef", "id": "macos-studio",
            "name": "Studio", "macos_version": "tahoe", "vcpus": 8,
            "ram_gb": 16, "disk_size_gb": 64, "auto_start": False,
            "selected_gpu_bdf": None, "selected_gpu_uuid": None,
            "installer_policy": "none", "fullscreen": True,
            "opencore_show_picker": False,
        }
        with patch.object(tui_module, "call_agent", return_value={"job_id": "job-123"}) as rpc:
            self.assertEqual(tui_module.VMTUI._submit_vm_provision_job(request), "job-123")
        self.assertEqual(rpc.call_args.args[0], "vms.provision")
        self.assertEqual(rpc.call_args.args[1], request)
        with patch.object(tui_module, "call_agent", return_value={}):
            with self.assertRaises(WebAgentUnavailable):
                tui_module.VMTUI._submit_vm_provision_job(request)


if __name__ == "__main__":
    unittest.main()
