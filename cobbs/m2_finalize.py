"""Finalize per-package report records, diffs, and evaluation scoring."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .m2_diff import save_diff
from .m2_metadata import _scoring_metadata_for_package
from .m2_resume import _resume_state
from .m2_scoring import _apply_scoring

INPUT_PRICE_PER_MILLION = 0.0
OUTPUT_PRICE_PER_MILLION = 0.0


def finalize_attempt(
    attempt: dict[str, Any], stop_reason: str, final_remaining: list[str],
    final_audit_succeeded: bool, final_tests_passed: bool,
    agent_statuses: list[str], all_agent_runs: list[dict[str, Any]],
    budget_stopped: bool, trace_files: list[str], original: Path, work: Path,
    direct_version: str | None, started: float, package: str,
    original_finding: dict[str, Any], report: dict[str, Any],
    scoring_metadata: dict[str, Any] | None,
) -> None:
    """Complete an attempt record and save its patch and scoring fields."""
    attempt["stop_reason"] = stop_reason
    attempt["vulns_remaining"] = final_remaining
    attempt["audit_succeeded"] = final_audit_succeeded
    attempt["proved_clean"] = bool(
        final_audit_succeeded and final_tests_passed and not final_remaining
    )
    attempt["agent_status"] = agent_statuses[-1] if agent_statuses else None
    if all_agent_runs:
        attempt["runs"] = all_agent_runs
    attempt["estimated_cost_usd"] = (
        attempt["input_tokens"] * INPUT_PRICE_PER_MILLION
        + attempt["output_tokens"] * OUTPUT_PRICE_PER_MILLION
    ) / 1_000_000
    attempt["completed"] = not budget_stopped
    attempt["trace_files"] = list(dict.fromkeys(trace_files))
    if attempt["completed"]:
        attempt["resume_state"] = _resume_state(original, work)
    if "result" not in attempt:
        if stop_reason == "clean":
            attempt["result"] = (
                "agent_fixed" if "fixed" in agent_statuses else "safe_upgrade"
            )
        else:
            attempt["result"] = stop_reason
    if direct_version is not None:
        patches = save_diff(original, work, package)
        if patches:
            attempt["patches"] = {
                kind: str(path) for kind, path in patches.items()
            }
    _apply_scoring(
        attempt, original, work,
        _scoring_metadata_for_package(scoring_metadata, package),
        final_tests_passed, attempt["trace_files"],
    )
    attempt["seconds"] = round(time.perf_counter() - started, 3)
    print(
        f"  {package} {original_finding['version']} -> "
        f"{attempt['final_version']}: {attempt['result']}"
    )
    report["attempts"].append(attempt)
    if budget_stopped:
        report["budget_stopped"] = True
        report["not_run_cases"] = attempt.get("not_run_cases", [])
