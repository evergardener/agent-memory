from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any


def _compare(operator: str, value: float, threshold: float) -> bool:
    if operator == "eq":
        return value == threshold
    if operator == "lte":
        return value <= threshold
    if operator == "gte":
        return value >= threshold
    raise ValueError(f"unsupported gate operator: {operator}")


def _metric_score(rule: dict[str, Any], value: float) -> float:
    mode = str(rule["mode"])
    target = float(rule["target"])
    if mode == "higher":
        if target <= 0:
            raise ValueError("higher metric target must be positive")
        return min(100.0, max(0.0, value / target * 100.0))
    if mode == "lower":
        zero_score_at = float(rule["zero_score_at"])
        if zero_score_at <= target:
            raise ValueError("lower metric zero_score_at must exceed target")
        if value <= target:
            return 100.0
        if value >= zero_score_at:
            return 0.0
        return (zero_score_at - value) / (zero_score_at - target) * 100.0
    if mode == "exact":
        return 100.0 if value == target else 0.0
    raise ValueError(f"unsupported metric scoring mode: {mode}")


def _measurement_value(rule: dict[str, Any], measurement: dict[str, Any], item_id: str) -> float:
    value = float(measurement["value"])
    if not math.isfinite(value):
        raise ValueError(f"measurement {item_id} must be finite")
    if "minimum" in rule and value < float(rule["minimum"]):
        raise ValueError(f"measurement {item_id} is below its minimum")
    if "maximum" in rule and value > float(rule["maximum"]):
        raise ValueError(f"measurement {item_id} exceeds its maximum")
    return value


def evaluate_run(spec: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    benchmark_id = str(spec["benchmark_id"])
    if run.get("benchmark_id") != benchmark_id:
        raise ValueError("run benchmark_id does not match the specification")

    gate_rules = {str(item["id"]): item for item in spec["hard_gates"]}
    metric_rules = {str(item["id"]): item for item in spec["metrics"]}
    supplied_gates = run.get("hard_gates", {})
    supplied_metrics = run.get("metrics", {})
    unknown_gates = sorted(set(supplied_gates) - set(gate_rules))
    unknown_metrics = sorted(set(supplied_metrics) - set(metric_rules))
    if unknown_gates:
        raise ValueError(f"unknown hard gate measurements: {', '.join(unknown_gates)}")
    if unknown_metrics:
        raise ValueError(f"unknown metric measurements: {', '.join(unknown_metrics)}")

    gate_results: list[dict[str, Any]] = []
    for gate_id, rule in gate_rules.items():
        measurement = supplied_gates.get(gate_id)
        if measurement is None:
            gate_results.append(
                {
                    "id": gate_id,
                    "name": rule["name"],
                    "status": "not_measured",
                    "required": bool(rule.get("required", True)),
                }
            )
            continue
        sample_count = int(measurement.get("sample_count", 0))
        if sample_count <= 0:
            raise ValueError(f"hard gate {gate_id} requires a positive sample_count")
        value = _measurement_value(rule, measurement, gate_id)
        passed = _compare(str(rule["operator"]), value, float(rule["threshold"]))
        gate_results.append(
            {
                "id": gate_id,
                "name": rule["name"],
                "status": "pass" if passed else "fail",
                "required": bool(rule.get("required", True)),
                "value": value,
                "sample_count": sample_count,
                "evidence": list(measurement.get("evidence", [])),
            }
        )

    total_weight = sum(float(item["weight"]) for item in metric_rules.values())
    if round(total_weight, 8) != 100.0:
        raise ValueError(f"metric weights must total 100, got {total_weight}")

    metric_results: list[dict[str, Any]] = []
    measured_weight = 0.0
    weighted_points = 0.0
    for metric_id, rule in metric_rules.items():
        measurement = supplied_metrics.get(metric_id)
        weight = float(rule["weight"])
        if measurement is None:
            metric_results.append(
                {
                    "id": metric_id,
                    "name": rule["name"],
                    "dimension": rule["dimension"],
                    "weight": weight,
                    "status": "not_measured",
                    "required": bool(rule.get("required", True)),
                }
            )
            continue
        sample_count = int(measurement.get("sample_count", 0))
        if sample_count <= 0:
            raise ValueError(f"metric {metric_id} requires a positive sample_count")
        value = _measurement_value(rule, measurement, metric_id)
        score = _metric_score(dict(rule["scoring"]), value)
        measured_weight += weight
        weighted_points += weight * score / 100.0
        metric_results.append(
            {
                "id": metric_id,
                "name": rule["name"],
                "dimension": rule["dimension"],
                "weight": weight,
                "status": "measured",
                "required": bool(rule.get("required", True)),
                "value": value,
                "sample_count": sample_count,
                "score": round(score, 4),
                "evidence": list(measurement.get("evidence", [])),
            }
        )

    failed_gates = [item["id"] for item in gate_results if item["status"] == "fail"]
    missing_required_gates = [
        item["id"]
        for item in gate_results
        if item["required"] and item["status"] == "not_measured"
    ]
    missing_required_metrics = [
        item["id"]
        for item in metric_results
        if item["required"] and item["status"] == "not_measured"
    ]
    measured_score = weighted_points / measured_weight * 100.0 if measured_weight else 0.0
    minimum_score = float(spec["release_policy"]["minimum_score"])
    if failed_gates:
        decision = "HARD_GATE_FAILED"
    elif missing_required_gates or missing_required_metrics:
        decision = "INCOMPLETE"
    elif measured_score < minimum_score:
        decision = "QUALITY_BELOW_THRESHOLD"
    else:
        decision = "PASS"

    return {
        "schema_version": "am-eval-result-v1",
        "benchmark_id": benchmark_id,
        "run_id": run["run_id"],
        "system": run["system"],
        "track": run["track"],
        "dataset": run["dataset"],
        "decision": decision,
        "release_ready": decision == "PASS",
        "hard_gate_summary": {
            "passed": sum(item["status"] == "pass" for item in gate_results),
            "failed": len(failed_gates),
            "not_measured": sum(item["status"] == "not_measured" for item in gate_results),
            "failed_ids": failed_gates,
            "missing_required_ids": missing_required_gates,
        },
        "quality_summary": {
            "measured_score": round(measured_score, 4),
            "coverage_percent": round(measured_weight, 4),
            "weighted_points": round(weighted_points, 4),
            "minimum_score": minimum_score,
            "missing_required_ids": missing_required_metrics,
        },
        "hard_gates": gate_results,
        "metrics": metric_results,
        "notes": list(run.get("notes", [])),
    }


def render_markdown(result: dict[str, Any]) -> str:
    quality = result["quality_summary"]
    gates = result["hard_gate_summary"]
    lines = [
        f"# {result['benchmark_id']} evaluation result",
        "",
        f"- Run: `{result['run_id']}`",
        f"- System: `{result['system']['name']} {result['system']['version']}`",
        f"- Revision: `{result['system']['revision']}`",
        f"- Track: `{result['track']}`",
        f"- Decision: `{result['decision']}`",
        f"- Measured score: `{quality['measured_score']:.2f}`",
        f"- Coverage: `{quality['coverage_percent']:.2f}%`",
        (
            f"- Hard gates: `{gates['passed']} passed / {gates['failed']} failed / "
            f"{gates['not_measured']} not measured`"
        ),
        "",
        "## Hard gates",
        "",
        "| ID | Gate | Status | Value |",
        "| --- | --- | --- | ---: |",
    ]
    for item in result["hard_gates"]:
        lines.append(
            f"| {item['id']} | {item['name']} | {item['status']} | "
            f"{item.get('value', '—')} |"
        )
    lines.extend(
        [
            "",
            "## Quality metrics",
            "",
            "| ID | Dimension | Metric | Status | Value | Score | Weight |",
            "| --- | --- | --- | --- | ---: | ---: | ---: |",
        ]
    )
    for item in result["metrics"]:
        lines.append(
            f"| {item['id']} | {item['dimension']} | {item['name']} | "
            f"{item['status']} | {item.get('value', '—')} | "
            f"{item.get('score', '—')} | {item['weight']} |"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate a run against an AM-Eval spec.")
    parser.add_argument("spec", type=Path)
    parser.add_argument("run", type=Path)
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    arguments = parser.parse_args()
    spec = json.loads(arguments.spec.read_text(encoding="utf-8"))
    run = json.loads(arguments.run.read_text(encoding="utf-8"))
    result = evaluate_run(spec, run)
    if arguments.format == "markdown":
        print(render_markdown(result), end="")
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
