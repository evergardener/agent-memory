import importlib
import json
import os
import re
import shutil
import tempfile
import time
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent.memory_manager import MemoryManager

from integrations.hermes.agent_memory.provider import AgentMemoryProvider


class LiveHermesProviderTests(unittest.TestCase):
    def test_memory_manager_writes_and_cross_profile_recalls(self) -> None:
        run_id = uuid.uuid4().hex[:12]
        personal = AgentMemoryProvider()
        personal_manager = MemoryManager()
        personal_manager.add_provider(personal)
        personal_manager.initialize_all(
            session_id=f"personal-{run_id}",
            agent_identity="personal",
            hermes_home="/tmp/agent-memory-live-personal",
        )
        personal_manager.on_turn_start(1, f"Project Nebula-{run_id}")
        personal_manager.sync_all(
            f"project:Nebula-{run_id} uses service:relay-{run_id}.",
            "Recorded the deployment decision.",
            session_id=f"personal-{run_id}",
            messages=[
                {
                    "role": "tool",
                    "name": "health_probe",
                    "content": f"service:relay-{run_id} health passed",
                }
            ],
        )
        personal_manager.flush_pending(timeout=5)

        coding = AgentMemoryProvider()
        coding.initialize(f"coding-{run_id}", agent_identity="coding")
        for _ in range(40):
            recalled = coding.prefetch(f"Nebula-{run_id} relay-{run_id}")
            if f"Nebula-{run_id}" in recalled:
                break
            time.sleep(0.25)
        else:
            self.fail("formal provider did not recall persisted cross-profile evidence")
        self.assertIn("profile: personal", recalled)
        self.assertIn("sources:", recalled)

        memory_match = re.search(r"memory: ([0-9a-f-]+)", recalled)
        self.assertIsNotNone(memory_match)
        memory_id = memory_match.group(1)
        traced = json.loads(
            coding.handle_tool_call("agent_memory_trace_source", {"memory_id": memory_id})
        )
        self.assertTrue(traced["evidence"])

        corrected_statement = f"project:Nebula-{run_id} uses service:relay-new-{run_id}."
        corrected = json.loads(
            coding.handle_tool_call(
                "agent_memory_correct",
                {
                    "memory_id": memory_id,
                    "corrected_statement": corrected_statement,
                    "reason": "integration correction",
                },
            )
        )
        self.assertEqual(corrected["state"], "superseded")
        corrected_recall = coding.prefetch(f"relay-new-{run_id}")
        self.assertIn(corrected_statement, corrected_recall)

        vault_secret = f"live-vault-secret-{run_id}"
        entry = personal.client.post(
            "/api/v1/vault/entries",
            {
                "context": personal._context(),
                "kind": "credential",
                "display_label": f"Live provider {run_id}",
                "redacted_hint": f"secret …{run_id[-4:]}",
                "secret_value": vault_secret,
            },
        )
        personal.client.post(
            f"/api/v1/vault/entries/{entry['entry_id']}/grants",
            {
                "context": personal._context(),
                "operation": "reveal_to_model",
                "target_profile": "coding",
                "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                "reason": "live provider explicit grant",
            },
        )
        protected = json.loads(
            coding.handle_tool_call(
                "agent_memory_use_protected_resource",
                {"entry_id": entry["entry_id"], "operation": "reveal_to_model"},
            )
        )
        self.assertTrue(protected["authorized"])
        self.assertEqual(protected["secret_value"], vault_secret)

        state = json.loads(coding.handle_tool_call("agent_memory_current_state", {}))
        self.assertIn("interaction", state)
        expiry = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        updated_state = json.loads(
            coding.handle_tool_call(
                "agent_memory_update_current_state",
                {
                    "action": "set",
                    "topic_key": f"live-state-{run_id}",
                    "summary": "Live provider state",
                    "expires_at": expiry,
                    "reason": "live provider integration",
                },
            )
        )
        self.assertEqual(updated_state["status"], "active")
        self.assertIn("Agent Memory continuity", coding.on_pre_compress([]))

    def test_provider_fails_soft_when_api_is_unavailable(self) -> None:
        old_url = os.environ.get("AGENT_MEMORY_API_URL")
        os.environ["AGENT_MEMORY_API_URL"] = "http://127.0.0.1:1"
        try:
            provider = AgentMemoryProvider()
            provider.initialize("offline-session", agent_identity="offline")
            provider.on_turn_start(1, "offline")
            self.assertEqual(provider.prefetch("anything"), "")
            provider.sync_turn("user", "assistant")
        finally:
            if old_url is None:
                os.environ.pop("AGENT_MEMORY_API_URL", None)
            else:
                os.environ["AGENT_MEMORY_API_URL"] = old_url

    def test_formal_plugin_is_discoverable_from_isolated_home(self) -> None:
        plugin_source = Path(__file__).resolve().parents[1] / "agent_memory"
        with tempfile.TemporaryDirectory() as temp_home:
            target = Path(temp_home) / "plugins" / "agent_memory"
            target.parent.mkdir(parents=True)
            shutil.copytree(plugin_source, target)
            old_home = os.environ.get("HERMES_HOME")
            os.environ["HERMES_HOME"] = temp_home
            try:
                import plugins.memory as memory_plugins

                importlib.reload(memory_plugins)
                providers = {
                    name: available
                    for name, _description, available in memory_plugins.discover_memory_providers()
                }
                self.assertTrue(providers.get("agent_memory"))
            finally:
                if old_home is None:
                    os.environ.pop("HERMES_HOME", None)
                else:
                    os.environ["HERMES_HOME"] = old_home


if __name__ == "__main__":
    unittest.main()
