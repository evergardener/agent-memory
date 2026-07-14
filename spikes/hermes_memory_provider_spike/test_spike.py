from __future__ import annotations

import importlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from agent.memory_manager import MemoryManager

from spikes.hermes_memory_provider_spike.plugin.provider import AgentMemoryProbeProvider, ProbeStore


class HermesMemoryProviderSpikeTests(unittest.TestCase):
    def test_memory_manager_lifecycle_recall_and_tool_trace(self) -> None:
        store = ProbeStore()
        provider = AgentMemoryProbeProvider(store=store, shared_namespace="hermes:user")
        manager = MemoryManager()
        manager.add_provider(provider)
        manager.initialize_all(
            session_id="session-personal", agent_identity="personal", hermes_home="/tmp/probe"
        )

        manager.on_turn_start(1, "Deploy Atlas")
        manager.sync_all(
            "We deployed Atlas to the intranet.",
            "Atlas deployment is complete.",
            session_id="session-personal",
            messages=[{"role": "tool", "content": "health check: ok"}],
        )
        manager.flush_pending(timeout=5)

        recall = provider.prefetch(
            "What is the Atlas deployment status?", session_id="session-personal"
        )
        self.assertIn("Atlas", recall)
        self.assertIn("source: evt_1", recall)
        self.assertTrue(any(item.kind == "tool_result" for item in store.evidence))

        traced = json.loads(
            provider.handle_tool_call("agent_memory_trace_source", {"evidence_id": "evt_1"})
        )
        self.assertEqual(traced["kind"], "user_message")
        self.assertIn("Atlas", traced["content"])
        self.assertTrue(any(event["event"] == "turn_start" for event in store.events))

    def test_profiles_share_explicit_namespace_but_keep_source_profile(self) -> None:
        store = ProbeStore()
        personal = AgentMemoryProbeProvider(store=store, shared_namespace="hermes:user")
        coding = AgentMemoryProbeProvider(store=store, shared_namespace="hermes:user")
        personal.initialize(
            "session-personal", agent_identity="personal", hermes_home="/tmp/personal"
        )
        personal.sync_turn(
            "The Aurora project uses a local relay.", "Noted.", session_id="session-personal"
        )
        coding.initialize("session-coding", agent_identity="coding", hermes_home="/tmp/coding")

        recall = coding.prefetch("Aurora relay", session_id="session-coding")
        self.assertIn("Aurora", recall)
        self.assertIn("profile: personal", recall)
        self.assertEqual({item.namespace for item in store.evidence}, {"hermes:user"})

    def test_session_switch_is_auditable(self) -> None:
        provider = AgentMemoryProbeProvider()
        provider.initialize("session-a", agent_identity="personal")
        provider.on_session_switch("session-b", parent_session_id="session-a", reset=True)

        self.assertEqual(provider.session_id, "session-b")
        switch = next(
            event for event in provider.store.events if event["event"] == "session_switch"
        )
        self.assertTrue(switch["reset"])

    def test_user_installed_provider_is_discovered_from_isolated_home(self) -> None:
        source_root = Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory() as temp_home:
            plugin_target = Path(temp_home) / "plugins" / "agent_memory_probe"
            plugin_target.parent.mkdir(parents=True)
            shutil.copytree(source_root / "plugin", plugin_target)
            old_home = os.environ.get("HERMES_HOME")
            os.environ["HERMES_HOME"] = temp_home
            try:
                import plugins.memory as memory_plugins

                importlib.reload(memory_plugins)
                found = dict(
                    (name, available)
                    for name, _description, available in memory_plugins.discover_memory_providers()
                )
                self.assertIn("agent_memory_probe", found)
                self.assertTrue(found["agent_memory_probe"])
            finally:
                if old_home is None:
                    os.environ.pop("HERMES_HOME", None)
                else:
                    os.environ["HERMES_HOME"] = old_home


if __name__ == "__main__":
    unittest.main()
