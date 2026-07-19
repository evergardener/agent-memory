from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .hermes_selection import is_automated_export_session
from .redaction import redact_structure_with_findings

NORMALIZER_VERSION = "hermes-backup-normalizer-v1"
ALLOWED_ROLES = {"user", "assistant", "tool", "session_meta"}
SESSION_FIELDS = (
    "id",
    "title",
    "started_at",
    "ended_at",
    "source",
    "profile_name",
    "display_name",
    "parent_session_id",
)
MESSAGE_FIELDS = (
    "id",
    "role",
    "content",
    "timestamp",
    "tool_calls",
    "tool_name",
    "tool_call_id",
)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_snapshot(path: Path) -> dict[str, Any]:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"Hermes backup snapshot does not exist: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError("invalid Hermes backup snapshot JSON") from error
    if not isinstance(value, dict):
        raise ValueError("Hermes backup snapshot must be a JSON object")
    if not isinstance(value.get("sessions"), list):
        raise ValueError("Hermes backup snapshot has no sessions array")
    if not isinstance(value.get("messages"), list):
        raise ValueError("Hermes backup snapshot has no messages array")
    return value


def normalize_backup_snapshot(
    source: Path, *, profile: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    source = source.expanduser().resolve()
    source_sha256 = _file_sha256(source)
    snapshot = _load_snapshot(source)
    sessions_by_id: dict[str, dict[str, Any]] = {}
    for raw_session in snapshot["sessions"]:
        if not isinstance(raw_session, dict):
            raise ValueError("Hermes backup contains a non-object session")
        session_id = str(raw_session.get("id") or "").strip()
        if not session_id:
            raise ValueError("Hermes backup contains a session without an id")
        if session_id in sessions_by_id:
            raise ValueError(f"Hermes backup contains duplicate session id: {session_id}")
        sessions_by_id[session_id] = raw_session

    messages_by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
    skipped = Counter()
    for raw_message in snapshot["messages"]:
        if not isinstance(raw_message, dict):
            skipped["non_object"] += 1
            continue
        session_id = str(raw_message.get("session_id") or "").strip()
        if session_id not in sessions_by_id:
            skipped["orphan"] += 1
            continue
        if raw_message.get("active") == 0:
            skipped["inactive"] += 1
            continue
        role = str(raw_message.get("role") or "").strip().casefold()
        if role == "system":
            skipped["system"] += 1
            continue
        if role not in ALLOWED_ROLES:
            skipped["unsupported_role"] += 1
            continue
        normalized_message = {
            key: raw_message.get(key)
            for key in MESSAGE_FIELDS
            if raw_message.get(key) is not None
        }
        normalized_message["role"] = role
        messages_by_session[session_id].append(normalized_message)

    normalized_sessions: list[dict[str, Any]] = []
    finding_counts: Counter[str] = Counter()
    sessions_without_messages = 0
    for session_id, raw_session in sessions_by_id.items():
        normalized = {
            key: raw_session.get(key)
            for key in SESSION_FIELDS
            if raw_session.get(key) is not None
        }
        normalized["id"] = session_id
        normalized["profile"] = profile
        normalized["messages"] = messages_by_session.get(session_id, [])
        if not normalized["messages"]:
            sessions_without_messages += 1
        redacted = redact_structure_with_findings(normalized)
        for finding in redacted.findings:
            finding_counts[finding.kind] += 1
        normalized_sessions.append(redacted.value)

    normalized_sessions.sort(
        key=lambda item: (str(item.get("started_at") or ""), str(item["id"]))
    )
    source_sha256_after = _file_sha256(source)
    if source_sha256_after != source_sha256:
        raise ValueError("Hermes backup snapshot changed while it was being normalized")
    manifest = {
        "normalizer_version": NORMALIZER_VERSION,
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "source_sha256": source_sha256,
        "source_size_bytes": source.stat().st_size,
        "source_session_count": len(sessions_by_id),
        "source_message_count": len(snapshot["messages"]),
        "normalized_session_count": len(normalized_sessions),
        "normalized_message_count": sum(
            len(session["messages"]) for session in normalized_sessions
        ),
        "sessions_without_messages": sessions_without_messages,
        "automated_session_count": sum(
            is_automated_export_session(session) for session in normalized_sessions
        ),
        "skipped_messages": dict(sorted(skipped.items())),
        "redaction_finding_count": sum(finding_counts.values()),
        "redaction_findings_by_kind": dict(sorted(finding_counts.items())),
        "contains_reasoning": False,
        "contains_system_prompt": False,
        "model_called": False,
        "external_data_sent": False,
        "source_modified": source_sha256_after != source_sha256,
    }
    return normalized_sessions, manifest


def _atomic_write(path: Path, content: str) -> Path:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    return path


def write_normalized_export(
    sessions: list[dict[str, Any]], manifest: dict[str, Any], *, output: Path, manifest_output: Path
) -> tuple[Path, Path, dict[str, Any]]:
    export_content = "".join(
        json.dumps(session, ensure_ascii=False, separators=(",", ":")) + "\n"
        for session in sessions
    )
    output = _atomic_write(output, export_content)
    completed_manifest = {
        **manifest,
        "normalized_sha256": _file_sha256(output),
        "normalized_size_bytes": output.stat().st_size,
    }
    manifest_output = _atomic_write(
        manifest_output,
        json.dumps(completed_manifest, ensure_ascii=False, indent=2) + "\n",
    )
    return output, manifest_output, completed_manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Normalize a read-only Hermes backup snapshot into redacted session JSONL."
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--manifest-output", required=True, type=Path)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        sessions, manifest = normalize_backup_snapshot(args.source, profile=args.profile)
        output, manifest_output, completed = write_normalized_export(
            sessions,
            manifest,
            output=args.output,
            manifest_output=args.manifest_output,
        )
    except ValueError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2) from error
    print(
        json.dumps(
            {
                **completed,
                "output": str(output),
                "manifest_output": str(manifest_output),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
