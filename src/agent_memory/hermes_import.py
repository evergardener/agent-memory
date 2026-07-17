from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .redaction import redact_structure_with_findings, redact_text

IMPORTER_VERSION = "hermes-session-jsonl-v1"
INTERNAL_MEMORY_TOOL_PREFIX = "agent_memory_"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


@dataclass(frozen=True)
class ImportPlan:
    source_path: Path
    source_sha256: str
    namespace: str
    profile: str
    turns: tuple[dict[str, Any], ...]
    manifest: dict[str, Any]


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_sessions(path: Path) -> list[dict[str, Any]]:
    sessions: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as stream:
        for line_number, raw_line in enumerate(stream, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"invalid Hermes JSONL at line {line_number}") from error
            if not isinstance(value, dict) or not isinstance(value.get("messages"), list):
                raise ValueError(f"line {line_number} is not a full Hermes session export")
            if not str(value.get("id") or value.get("session_id") or "").strip():
                raise ValueError(f"line {line_number} has no Hermes session id")
            sessions.append(value)
    if not sessions:
        raise ValueError("Hermes export contains no sessions")
    return sessions


def _message_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [_message_text(item) for item in content]
        return "\n".join(part for part in parts if part)
    if isinstance(content, dict):
        for key in ("text", "content"):
            if isinstance(content.get(key), str):
                return str(content[key])
        return json.dumps(content, ensure_ascii=False, sort_keys=True)
    return str(content)


def _timestamp(value: Any, fallback: datetime) -> datetime:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=UTC)
    if isinstance(value, str) and value.strip():
        normalized = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return fallback
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    return fallback


def _arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {"unparsed": value}
    if isinstance(value, dict):
        return value
    return {"value": value}


def _safe_external_id(value: str, *, prefix: str) -> str:
    if len(value) <= 480:
        return value
    digest = hashlib.sha256(value.encode()).hexdigest()
    return f"{prefix}:{digest}"


def _event_findings(event: dict[str, Any]) -> list[str]:
    findings = [
        finding.kind
        for finding in redact_text(str(event.get("content") or "")).findings
    ]
    if event.get("arguments") is not None:
        structured = redact_structure_with_findings(event["arguments"])
        findings.extend(finding.kind for finding in structured.findings)
    return findings


def _session_turns(
    session: dict[str, Any], *, namespace: str, profile: str, fallback: datetime
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    session_id = str(session.get("id") or session.get("session_id"))
    external_session_id = _safe_external_id(
        f"hermes-export:{session_id}", prefix="hermes-export-session"
    )
    session_fallback = _timestamp(session.get("started_at"), fallback)
    turns: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    turn_time = session_fallback
    turn_key = ""
    turn_index = 0
    skipped = {"system": 0, "orphan": 0, "empty": 0, "internal_tool": 0}

    def append_event(event: dict[str, Any]) -> None:
        event["sequence"] = len(events) + 1
        events.append(event)

    def flush() -> None:
        nonlocal events, turn_key, turn_time
        if not events:
            return
        identity = f"{namespace}:{profile}:{session_id}:{turn_key}"
        identity_hash = hashlib.sha256(identity.encode()).hexdigest()
        idempotency_key = f"hermes-import:{identity_hash}"
        turns.append(
            {
                "context": {
                    "shared_namespace": namespace,
                    "source_profile": profile,
                    "source_instance": "hermes-session-export",
                    "external_session_id": external_session_id,
                    "external_turn_id": _safe_external_id(
                        f"import:{turn_key}", prefix="hermes-export-turn"
                    ),
                    "correlation_id": str(uuid.uuid5(uuid.NAMESPACE_URL, idempotency_key)),
                },
                "idempotency_key": idempotency_key,
                "occurred_at": turn_time.isoformat(),
                "events": events,
            }
        )
        events = []

    for message in session.get("messages") or []:
        if not isinstance(message, dict):
            skipped["empty"] += 1
            continue
        role = str(message.get("role") or "").lower()
        if role == "system":
            skipped["system"] += 1
            continue
        if role == "user":
            flush()
            turn_index += 1
            turn_key = str(message.get("id") or message.get("message_id") or turn_index)
            turn_time = _timestamp(message.get("timestamp"), session_fallback)
            content = _message_text(message.get("content"))
            if not content.strip():
                skipped["empty"] += 1
                continue
            append_event({"type": "user_message", "content": content})
            continue
        if not events:
            skipped["orphan"] += 1
            continue
        if role == "assistant":
            content = _message_text(message.get("content"))
            if content.strip():
                append_event({"type": "assistant_message", "content": content})
            for tool_call in message.get("tool_calls") or []:
                if not isinstance(tool_call, dict):
                    continue
                function = tool_call.get("function") or tool_call
                tool_name = str(function.get("name") or "unknown")
                if tool_name.startswith(INTERNAL_MEMORY_TOOL_PREFIX):
                    skipped["internal_tool"] += 1
                    continue
                append_event(
                    {
                        "type": "tool_call",
                        "content": "",
                        "tool_name": tool_name,
                        "arguments": _arguments(function.get("arguments") or {}),
                    }
                )
            if not content.strip() and not message.get("tool_calls"):
                skipped["empty"] += 1
            continue
        if role == "tool":
            tool_name = str(message.get("tool_name") or message.get("name") or "unknown")
            if tool_name.startswith(INTERNAL_MEMORY_TOOL_PREFIX):
                skipped["internal_tool"] += 1
                continue
            append_event(
                {
                    "type": "tool_result",
                    "content": _message_text(message.get("content")),
                    "tool_name": tool_name,
                }
            )
            continue
        skipped["orphan"] += 1
    flush()
    return turns, skipped


def plan_import(path: Path, *, namespace: str, profile: str) -> ImportPlan:
    source_path = path.expanduser().resolve()
    if not source_path.is_file():
        raise ValueError(f"Hermes export does not exist: {source_path}")
    source_sha256 = _file_sha256(source_path)
    sessions = _load_sessions(source_path)
    fallback = datetime.fromtimestamp(source_path.stat().st_mtime, tz=UTC)
    turns: list[dict[str, Any]] = []
    skipped = {"system": 0, "orphan": 0, "empty": 0, "internal_tool": 0}
    for session in sessions:
        session_turns, session_skipped = _session_turns(
            session, namespace=namespace, profile=profile, fallback=fallback
        )
        turns.extend(session_turns)
        for key, value in session_skipped.items():
            skipped[key] += value
    event_counts: dict[str, int] = {}
    finding_counts: dict[str, int] = {}
    finding_count = 0
    event_count = 0
    occurred_values: list[str] = []
    for turn in turns:
        occurred_values.append(turn["occurred_at"])
        for event in turn["events"]:
            event_count += 1
            event_counts[event["type"]] = event_counts.get(event["type"], 0) + 1
            for finding_kind in _event_findings(event):
                finding_count += 1
                finding_counts[finding_kind] = finding_counts.get(finding_kind, 0) + 1
    manifest = {
        "importer_version": IMPORTER_VERSION,
        "source_sha256": source_sha256,
        "source_size_bytes": source_path.stat().st_size,
        "namespace": namespace,
        "profile": profile,
        "session_count": len(sessions),
        "turn_count": len(turns),
        "event_count": event_count,
        "event_counts": event_counts,
        "potential_sensitive_findings": finding_count,
        "potential_sensitive_findings_by_kind": finding_counts,
        "skipped_messages": skipped,
        "occurred_at_min": min(occurred_values) if occurred_values else None,
        "occurred_at_max": max(occurred_values) if occurred_values else None,
    }
    return ImportPlan(
        source_path=source_path,
        source_sha256=source_sha256,
        namespace=namespace,
        profile=profile,
        turns=tuple(turns),
        manifest=manifest,
    )


def _validate_apply(
    plan: ImportPlan,
    *,
    api_url: str,
    confirm_sha256: str,
    allow_primary: bool,
    accept_local_redaction: bool,
) -> None:
    parsed = urlparse(api_url)
    if parsed.scheme != "http" or parsed.hostname not in LOOPBACK_HOSTS:
        raise ValueError("Hermes history import only permits a local HTTP API")
    if confirm_sha256 != plan.source_sha256:
        raise ValueError("--confirm-sha256 must exactly match the previewed source SHA-256")
    if plan.namespace == "hermes:user-primary" and not allow_primary:
        raise ValueError("primary namespace import requires --allow-primary")
    if plan.manifest["potential_sensitive_findings"] and not accept_local_redaction:
        raise ValueError(
            "potential sensitive values found; export with Hermes --redact or pass "
            "--accept-local-redaction after reviewing the local source file"
        )


def _post_turn(api_url: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        api_url.rstrip("/") + "/api/v1/ingest/turn",
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"Agent Memory import API rejected a turn: HTTP {error.code}") from error
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        raise RuntimeError("Agent Memory import API unavailable") from error


def apply_import(plan: ImportPlan, *, api_url: str, token: str) -> dict[str, Any]:
    inserted = 0
    duplicates = 0
    event_count = 0
    for turn in plan.turns:
        result = _post_turn(api_url, token, turn)
        if result.get("duplicate"):
            duplicates += 1
        else:
            inserted += 1
            event_count += len(result.get("event_ids") or [])
    return {
        **plan.manifest,
        "status": "applied",
        "inserted_turns": inserted,
        "duplicate_turns": duplicates,
        "accepted_events": event_count,
        "applied_at": datetime.now(UTC).isoformat(),
    }


def _write_manifest(result: dict[str, Any], directory: Path) -> Path:
    batch_directory = directory / result["source_sha256"]
    batch_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(batch_directory, 0o700)
    applied_at = str(result.get("applied_at") or datetime.now(UTC).isoformat())
    timestamp = applied_at.replace(":", "").replace("-", "").replace(".", "")
    target = batch_directory / f"{timestamp}-{uuid.uuid4().hex[:12]}.json"
    temporary = directory / f".{target.name}.{os.getpid()}.tmp"
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    os.chmod(temporary, 0o600)
    shutil.move(temporary, target)
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description="Preview or import Hermes session JSONL")
    parser.add_argument("source", type=Path)
    parser.add_argument("--namespace", default="hermes:import-staging")
    parser.add_argument("--profile", default="historical-import")
    parser.add_argument("--api-url", default="http://127.0.0.1:7790")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-sha256", default="")
    parser.add_argument("--allow-primary", action="store_true")
    parser.add_argument("--accept-local-redaction", action="store_true")
    parser.add_argument(
        "--manifest-dir", type=Path, default=Path("data/imports/manifests")
    )
    args = parser.parse_args()
    try:
        plan = plan_import(args.source, namespace=args.namespace, profile=args.profile)
        if not args.apply:
            print(json.dumps({**plan.manifest, "status": "preview"}, ensure_ascii=False, indent=2))
            return
        _validate_apply(
            plan,
            api_url=args.api_url,
            confirm_sha256=args.confirm_sha256,
            allow_primary=args.allow_primary,
            accept_local_redaction=args.accept_local_redaction,
        )
        token = os.getenv("AGENT_MEMORY_SERVICE_TOKEN", "")
        if not token:
            raise ValueError("AGENT_MEMORY_SERVICE_TOKEN is required for --apply")
        result = apply_import(plan, api_url=args.api_url, token=token)
        manifest_path = _write_manifest(result, args.manifest_dir)
        print(json.dumps({**result, "manifest_path": str(manifest_path)}, ensure_ascii=False))
    except (ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2) from error


if __name__ == "__main__":
    main()
