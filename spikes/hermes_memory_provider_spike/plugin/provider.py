"""Non-production Hermes MemoryProvider used only for integration validation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from agent.memory_provider import MemoryProvider


@dataclass
class ProbeEvidence:
    id: str
    namespace: str
    session_id: str
    profile: str
    kind: str
    content: str
    created_at: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProbeStore:
    """A deliberately ephemeral store shared by provider instances in tests."""

    evidence: list[ProbeEvidence] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)


class AgentMemoryProbeProvider(MemoryProvider):
    """Validates Hermes lifecycle wiring without persisting any user data."""

    def __init__(
        self, *, store: ProbeStore | None = None, shared_namespace: str = "hermes:default"
    ):
        self.store = store or ProbeStore()
        self.shared_namespace = shared_namespace
        self.session_id = ""
        self.profile = ""

    @property
    def name(self) -> str:
        return "agent_memory_probe"

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        self.session_id = session_id
        self.profile = str(kwargs.get("agent_identity") or "default")
        self.store.events.append(
            {
                "event": "initialize",
                "session_id": session_id,
                "profile": self.profile,
                "namespace": self.shared_namespace,
            }
        )

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "agent_memory_recall",
                "description": "Recall evidence captured by the agent-memory provider.",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
            {
                "name": "agent_memory_trace_source",
                "description": "Trace a recalled evidence item by its id.",
                "parameters": {
                    "type": "object",
                    "properties": {"evidence_id": {"type": "string"}},
                    "required": ["evidence_id"],
                },
            },
        ]

    def on_turn_start(self, turn_number: int, message: str, **kwargs: Any) -> None:
        self.store.events.append(
            {
                "event": "turn_start",
                "turn": turn_number,
                "session_id": self.session_id,
                "message": message,
            }
        )

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: list[dict[str, Any]] | None = None,
    ) -> None:
        effective_session = session_id or self.session_id
        self._record("user_message", user_content, effective_session)
        self._record("assistant_message", assistant_content, effective_session)
        for message in messages or []:
            if message.get("role") == "tool":
                self._record("tool_result", str(message.get("content") or ""), effective_session)

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        tokens = set(re.findall(r"[\w-]+", query.lower()))
        matches = [
            item
            for item in self.store.evidence
            if item.namespace == self.shared_namespace
            and tokens.intersection(re.findall(r"[\w-]+", item.content.lower()))
        ]
        if not matches:
            return ""
        lines = ["[agent-memory probe recall]"]
        for item in matches[:3]:
            lines.append(f"- {item.content} (source: {item.id}, profile: {item.profile})")
        return "\n".join(lines)

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs: Any) -> str:
        if tool_name == "agent_memory_recall":
            return json.dumps({"context": self.prefetch(str(args["query"]))})
        if tool_name == "agent_memory_trace_source":
            evidence_id = str(args["evidence_id"])
            match = next((item for item in self.store.evidence if item.id == evidence_id), None)
            if match is None:
                return json.dumps({"error": "not_found"})
            return json.dumps({"id": match.id, "kind": match.kind, "content": match.content})
        return json.dumps({"error": "unsupported_tool"})

    def on_session_switch(self, new_session_id: str, **kwargs: Any) -> None:
        self.store.events.append(
            {"event": "session_switch", "from": self.session_id, "to": new_session_id, **kwargs}
        )
        self.session_id = new_session_id

    def on_session_end(self, messages: list[dict[str, Any]]) -> None:
        self.store.events.append(
            {"event": "session_end", "session_id": self.session_id, "messages": len(messages)}
        )

    def _record(self, kind: str, content: str, session_id: str) -> None:
        evidence_id = f"evt_{len(self.store.evidence) + 1}"
        self.store.evidence.append(
            ProbeEvidence(
                id=evidence_id,
                namespace=self.shared_namespace,
                session_id=session_id,
                profile=self.profile,
                kind=kind,
                content=content,
                created_at=datetime.now(UTC).isoformat(),
            )
        )
