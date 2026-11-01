from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from core.web_client import WebAgentUnavailable, agent_is_required, call_agent, list_agent_vms


class WebAgentClientTests(unittest.TestCase):
    def test_appliance_flag_requires_agent_instead_of_local_fallback(self):
        with patch.dict(os.environ, {"VHACKINTOSH_AGENT_REQUIRED": "1"}, clear=False):
            self.assertTrue(agent_is_required())

    def test_missing_agent_is_reported_with_recovery_hint(self):
        with patch.dict(os.environ, {
            "VHACKINTOSH_AGENT_SOCKET": "/tmp/vhackintosh-agent-test-missing.sock",
        }, clear=False):
            with self.assertRaises(WebAgentUnavailable) as caught:
                call_agent("agent.health")
        self.assertIn("serviço de gerenciamento local está indisponível", str(caught.exception))
        self.assertIn("vhackintosh-agent", str(caught.exception))

    def test_catalog_helper_validates_and_parses_agent_owned_config(self):
        config = {
            "id": "vm-a", "name": "Mac Studio", "macos_version": "tahoe",
            "revision": "field-revision-should-not-be-persisted",
        }
        with patch("core.web_client.call_agent", return_value={
            "revision": "catalog-r1",
            "items": [{"config": config, "revision": "catalog-r1"}],
        }):
            vms = list_agent_vms()
        self.assertEqual([vm.id for vm in vms], ["vm-a"])
        self.assertNotIn("revision", vms[0].extra_config)

    def test_invalid_agent_catalog_is_reported_without_fallback(self):
        with patch("core.web_client.call_agent", return_value={"revision": "r1", "items": [None]}):
            with self.assertRaisesRegex(WebAgentUnavailable, "configuração de VM inválida"):
                list_agent_vms()


if __name__ == "__main__":
    unittest.main()
