import json
import logging
import os
import socket
import uuid
from datetime import UTC, datetime
from typing import Any

from agent.memory_provider import MemoryProvider

from .client import AgentMemoryHttpClient, ApiResponseError, ApiUnavailable

logger = logging.getLogger(__name__)


class AgentMemoryProvider(MemoryProvider):
    """Fail-soft Hermes adapter; persistent memory remains owned by the local API."""

    def __init__(self, *, client: AgentMemoryHttpClient | None = None):
        token = os.getenv("AGENT_MEMORY_SERVICE_TOKEN", "")
        self.client = client or AgentMemoryHttpClient(
            base_url=os.getenv("AGENT_MEMORY_API_URL", "http://127.0.0.1:7788"),
            service_token=token,
            timeout_seconds=float(os.getenv("AGENT_MEMORY_API_TIMEOUT_SECONDS", "2")),
        )
        self.shared_namespace = os.getenv("AGENT_MEMORY_NAMESPACE", "hermes:user-primary")
        self.configured_profile = os.getenv("AGENT_MEMORY_SOURCE_PROFILE", "")
        self.source_instance = os.getenv("AGENT_MEMORY_SOURCE_INSTANCE", socket.gethostname())
        self.session_id = ""
        self.profile = "default"
        self.turn_number = 0

    @property
    def name(self) -> str:
        return "agent_memory"

    def is_available(self) -> bool:
        return bool(self.client.service_token)

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        self.session_id = session_id
        self.profile = self.configured_profile or str(kwargs.get("agent_identity") or "default")
        self.turn_number = 0

    def system_prompt_block(self) -> str:
        return (
            "Agent Memory provides evidence-linked long-term recall. "
            "Treat recalled items as context with source references, not infallible truth."
        )

    def on_turn_start(self, turn_number: int, message: str, **kwargs: Any) -> None:
        self.turn_number = turn_number

    def _context(self, session_id: str = "") -> dict[str, Any]:
        effective_session = session_id or self.session_id or "unknown-session"
        return {
            "shared_namespace": self.shared_namespace,
            "source_profile": self.profile,
            "source_instance": self.source_instance,
            "external_session_id": effective_session,
            "external_turn_id": f"turn-{self.turn_number}",
            "correlation_id": str(uuid.uuid4()),
        }

    def _recall(self, query: str, *, intent: str, session_id: str = "") -> str:
        payload = {
            "context": self._context(session_id),
            "query": query,
            "intent": intent,
            "budget": {"max_items": 8, "max_chars": 4200},
            "scopes": ["global", "project", "phase"],
        }
        try:
            result = self.client.post("/api/v1/recall", payload)
        except ApiUnavailable:
            logger.warning("Agent Memory recall unavailable; continuing without injected memory")
            return ""
        items = result.get("items") or []
        if not items:
            return ""
        lines = ["[Agent Memory recall — evidence-linked, may contain candidate facts]"]
        for item in items:
            sources = ",".join(item.get("source_ids") or [])
            lines.append(
                f"- {item.get('text', '')} "
                f"(memory: {item.get('memory_id')}, sources: {sources}, "
                f"profile: {item.get('source_profile')})"
            )
        return "\n".join(lines)

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        return self._recall(query, intent="conversation", session_id=session_id)

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: list[dict[str, Any]] | None = None,
    ) -> None:
        events: list[dict[str, Any]] = [
            {"type": "user_message", "sequence": 1, "content": user_content},
            {"type": "assistant_message", "sequence": 2, "content": assistant_content},
        ]
        sequence = 3
        for message in messages or []:
            for tool_call in message.get("tool_calls") or []:
                function = tool_call.get("function") or {}
                raw_arguments = function.get("arguments") or {}
                try:
                    arguments = (
                        json.loads(raw_arguments)
                        if isinstance(raw_arguments, str)
                        else raw_arguments
                    )
                except json.JSONDecodeError:
                    arguments = {"unparsed": str(raw_arguments)}
                events.append(
                    {
                        "type": "tool_call",
                        "sequence": sequence,
                        "content": "",
                        "tool_name": str(function.get("name") or "unknown"),
                        "arguments": arguments,
                    }
                )
                sequence += 1
            if message.get("role") == "tool":
                events.append(
                    {
                        "type": "tool_result",
                        "sequence": sequence,
                        "content": str(message.get("content") or ""),
                        "tool_name": str(message.get("name") or "unknown"),
                    }
                )
                sequence += 1
        effective_session = session_id or self.session_id or "unknown-session"
        payload = {
            "context": self._context(effective_session),
            "idempotency_key": (
                f"hermes:{self.shared_namespace}:{self.profile}:"
                f"{effective_session}:turn-{self.turn_number}"
            ),
            "occurred_at": datetime.now(UTC).isoformat(),
            "events": events,
        }
        try:
            self.client.post("/api/v1/ingest/turn", payload)
        except ApiUnavailable:
            logger.warning("Agent Memory ingest unavailable; Hermes turn remains available")

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "agent_memory_recall",
                "description": "Explicitly search evidence-linked long-term memory.",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
            {
                "name": "agent_memory_trace_source",
                "description": "Trace a recalled memory to its redacted source evidence.",
                "parameters": {
                    "type": "object",
                    "properties": {"memory_id": {"type": "string"}},
                    "required": ["memory_id"],
                },
            },
            {
                "name": "agent_memory_correct",
                "description": "Record an explicit user correction to a memory.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "memory_id": {"type": "string"},
                        "corrected_statement": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["memory_id", "corrected_statement", "reason"],
                },
            },
            {
                "name": "agent_memory_use_protected_resource",
                "description": (
                    "Use a Vault entry only after the user created an explicit, unexpired grant."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "entry_id": {"type": "string"},
                        "operation": {
                            "type": "string",
                            "enum": ["reveal_to_model"],
                        },
                    },
                    "required": ["entry_id", "operation"],
                },
            },
        ]

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs: Any) -> str:
        if tool_name == "agent_memory_recall":
            context = self._recall(str(args["query"]), intent="explicit")
            return json.dumps({"context": context}, ensure_ascii=False)
        if tool_name == "agent_memory_trace_source":
            try:
                result = self.client.get(
                    f"/api/v1/memory/{args['memory_id']}/trace",
                    {"shared_namespace": self.shared_namespace},
                )
            except ApiUnavailable:
                return json.dumps({"error": "service_unavailable"})
            return json.dumps(result, ensure_ascii=False)
        if tool_name == "agent_memory_correct":
            payload = {
                "context": self._context(),
                "corrected_statement": str(args["corrected_statement"]),
                "reason": str(args["reason"]),
            }
            try:
                result = self.client.post(
                    f"/api/v1/memory/{args['memory_id']}/corrections", payload
                )
            except ApiUnavailable:
                return json.dumps({"error": "service_unavailable"})
            return json.dumps(result, ensure_ascii=False)
        if tool_name == "agent_memory_use_protected_resource":
            payload = {
                "context": self._context(),
                "entry_id": str(args["entry_id"]),
                "operation": str(args["operation"]),
            }
            try:
                result = self.client.post("/api/v1/vault/requests", payload)
            except ApiResponseError as error:
                return json.dumps({"error": error.code.lower()})
            except ApiUnavailable:
                return json.dumps({"error": "service_unavailable"})
            return json.dumps(result, ensure_ascii=False)
        return json.dumps({"error": "unsupported_tool"})

    def on_session_switch(self, new_session_id: str, **kwargs: Any) -> None:
        self.session_id = new_session_id
        self.turn_number = 0
