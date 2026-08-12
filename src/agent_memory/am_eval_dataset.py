from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any


class DatasetError(ValueError):
    """Raised when an AM-Eval dataset fails its frozen-data contract."""


ALLOWED_CASE_SUITES = {
    "date_range",
    "episode",
    "preference",
    "procedure",
    "recall",
    "temporal_rule",
}
ALLOWED_SPLITS = {"development", "validation", "blind"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_jsonl(path: Path) -> tuple[dict[str, Any], ...]:
    cases: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    with path.open(encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                case = json.loads(line)
            except json.JSONDecodeError as error:
                raise DatasetError(f"invalid JSONL at {path}:{line_number}") from error
            if not isinstance(case, dict):
                raise DatasetError(f"case at {path}:{line_number} must be an object")
            if case.get("schema_version") != "am-eval-case-v1":
                raise DatasetError(f"unsupported case schema at {path}:{line_number}")
            case_id = str(case.get("case_id") or "")
            if not case_id or case_id in seen_ids:
                raise DatasetError(f"missing or duplicate case_id at {path}:{line_number}")
            suite = str(case.get("suite") or "")
            if suite not in ALLOWED_CASE_SUITES:
                raise DatasetError(f"unsupported suite {suite!r} at {path}:{line_number}")
            split = str(case.get("split") or "")
            if split not in ALLOWED_SPLITS:
                raise DatasetError(f"unsupported split {split!r} at {path}:{line_number}")
            if not isinstance(case.get("input"), dict) or not isinstance(
                case.get("expected"), dict
            ):
                raise DatasetError(f"case {case_id} requires input and expected objects")
            seen_ids.add(case_id)
            cases.append(case)
    if not cases:
        raise DatasetError(f"dataset file is empty: {path}")
    return tuple(cases)


def load_dataset(manifest_path: Path) -> tuple[dict[str, Any], ...]:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DatasetError(f"invalid dataset manifest: {manifest_path}") from error
    if manifest.get("schema_version") != "am-eval-dataset-manifest-v1":
        raise DatasetError("unsupported dataset manifest schema")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise DatasetError("dataset manifest must declare at least one file")

    root = manifest_path.parent
    all_cases: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for item in files:
        if not isinstance(item, dict):
            raise DatasetError("dataset manifest file entries must be objects")
        relative = str(item.get("path") or "")
        if not relative or relative in seen_paths:
            raise DatasetError(f"missing or duplicate dataset path: {relative!r}")
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError as error:
            raise DatasetError(f"dataset path escapes manifest root: {relative}") from error
        if not candidate.is_file():
            raise DatasetError(f"dataset file does not exist: {relative}")
        actual_sha256 = sha256_file(candidate)
        if actual_sha256 != item.get("sha256"):
            raise DatasetError(f"dataset SHA-256 mismatch: {relative}")
        cases = load_jsonl(candidate)
        if len(cases) != int(item.get("case_count", -1)):
            raise DatasetError(f"dataset case count mismatch: {relative}")
        declared_suites = set(item.get("suites") or [])
        actual_suites = {str(case["suite"]) for case in cases}
        if actual_suites != declared_suites:
            raise DatasetError(f"dataset suite declaration mismatch: {relative}")
        seen_paths.add(relative)
        all_cases.extend(cases)

    case_ids = [str(case["case_id"]) for case in all_cases]
    if len(case_ids) != len(set(case_ids)):
        raise DatasetError("case_id values must be unique across the dataset")
    expected_total = int(manifest.get("case_count", -1))
    if len(all_cases) != expected_total:
        raise DatasetError("dataset manifest total case count mismatch")
    declared_counts = manifest.get("suite_counts")
    actual_counts = Counter(str(case["suite"]) for case in all_cases)
    if declared_counts != dict(sorted(actual_counts.items())):
        raise DatasetError("dataset manifest suite counts mismatch")
    if manifest.get("contains_production_data") is not False:
        raise DatasetError("the public deterministic dataset must not contain production data")
    blind_count = sum(case["split"] == "blind" for case in all_cases)
    if blind_count != int(manifest.get("blind_cases", -1)):
        raise DatasetError("dataset manifest blind case count mismatch")
    if manifest.get("visibility") == "open" and blind_count:
        raise DatasetError("an open dataset cannot contain blind cases")
    return tuple(all_cases)


def dataset_summary(manifest_path: Path) -> dict[str, Any]:
    cases = load_dataset(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return {
        "schema_version": "am-eval-dataset-validation-v1",
        "dataset_id": manifest["dataset_id"],
        "manifest_sha256": sha256_file(manifest_path),
        "case_count": len(cases),
        "suite_counts": dict(sorted(Counter(case["suite"] for case in cases).items())),
        "split_counts": dict(sorted(Counter(case["split"] for case in cases).items())),
        "status": "PASS",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate a frozen AM-Eval JSONL dataset.")
    parser.add_argument("manifest", type=Path)
    arguments = parser.parse_args()
    print(
        json.dumps(
            dataset_summary(arguments.manifest),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
