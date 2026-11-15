"""Milestone-two iterative repair orchestration."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from .audit import audit_result
from .m2_metadata import _load_scoring_metadata
from .m2_attempts import process_findings
from .gemini import GeminiBackend
from .sandbox import install, make_sandbox, run_tests
from .versions import installed_versions

MAX_ROUNDS = 3
INPUT_PRICE_PER_MILLION = 0.0
OUTPUT_PRICE_PER_MILLION = 0.0

def run_repository(
    original: Path, backend: GeminiBackend, runs: int = 1,
    resume_attempts: list[dict[str, Any]] | None = None,
    scoring_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Upgrade each directly pinned vulnerable package in fresh bounded rounds."""
    if runs < 1:
        raise ValueError("runs must be at least 1")
    if scoring_metadata is None:
        scoring_metadata = _load_scoring_metadata(original)
    tmp, work, py = make_sandbox(original)
    report: dict[str, Any] = {
        "repo": str(original), "baseline": None, "attempts": [],
    }
    try:
        installed, log = install(py, work)
        if not installed:
            report["baseline"] = {"installed": False, "log": log}
            print("Baseline install failed.\n", log)
            return report

        latest_versions = installed_versions(py, work)
        passed, log = run_tests(py, work)
        report["baseline"] = {
            "installed": True, "tests_passed": passed, "log": log,
        }
        if not passed:
            print("Baseline tests already failing. Fix those first.")
            return report
        findings, audit_succeeded = audit_result(py, work)
        report["baseline"]["audit_succeeded"] = audit_succeeded
        if not audit_succeeded:
            raise RuntimeError("pip-audit returned invalid JSON for the baseline")
        print(f"Found {len(findings)} vulnerable package(s).")
        process_findings(
            original, backend, runs, report, work, findings, passed,
            latest_versions, resume_attempts, scoring_metadata,
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return report
