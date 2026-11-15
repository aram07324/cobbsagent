"""Per-package upgrade rounds and resume processing."""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any

from .audit import audit_result
from .m2_finalize import finalize_attempt
from .m2_metadata import (
    _package_key,
)
from .m2_repair import repair_broken_round
from .m2_resume import (
    _copy_repository, _newer_version, _package_finding, _requirement_version,
    restore_completed_attempt,
)
from .sandbox import install, make_sandbox, run_tests
from .versions import _version_key, bump_requirement, installed_versions

MAX_ROUNDS = 3

def process_findings(
    original: Path, backend: Any, runs: int, report: dict[str, Any], work: Path,
    findings: list[dict[str, Any]], passed: bool,
    latest_versions: dict[str, str],
    resume_attempts: list[dict[str, Any]] | None,
    scoring_metadata: dict[str, Any] | None,
) -> None:
    """Process vulnerable packages, including resume and bounded repair rounds."""
    direct_packages = {
        _package_key(finding["name"])
        for finding in findings
        if _requirement_version(work, finding["name"]) is not None
    }
    ordered_findings = [
        *[f for f in findings if _package_key(f["name"]) in direct_packages],
        *[f for f in findings if _package_key(f["name"]) not in direct_packages],
    ]
    latest_findings = findings
    latest_tests_passed = passed
    latest_audit_succeeded = True
    resolved_by: dict[str, str] = {}
    previous_by_package = {
        _package_key(item.get("name", "")): item
        for item in (resume_attempts or [])
        if item.get("name")
    }
    budget_stopped = False

    for finding_index, original_finding in enumerate(ordered_findings):
        started = time.perf_counter()
        package = original_finding["name"]
        package_key = _package_key(package)
        previous_attempt = previous_by_package.get(package_key)
        (
            previous_completed, latest_versions, latest_tests_passed,
            latest_findings, latest_audit_succeeded,
        ) = restore_completed_attempt(
            previous_attempt, runs, report, work, findings, direct_packages,
            package_key, package, latest_versions, latest_tests_passed,
            latest_findings, latest_audit_succeeded, resolved_by,
            make_sandbox, install, installed_versions, run_tests, audit_result,
        )
        if previous_completed:
            continue
        attempt = dict(original_finding)
        attempt["vuln_ids"] = list(dict.fromkeys(original_finding.get("vuln_ids", [])))
        attempt.update(
            agent_status=None,
            steps_used=int((previous_attempt or {}).get("steps_used", 0)),
            input_tokens=int((previous_attempt or {}).get("input_tokens", 0)),
            output_tokens=int((previous_attempt or {}).get("output_tokens", 0)),
            requests_used=int((previous_attempt or {}).get("requests_used", 0)),
            agent_requests=int((previous_attempt or {}).get("agent_requests", 0)),
            cache_hits=int((previous_attempt or {}).get("cache_hits", 0)),
            cached=bool((previous_attempt or {}).get("cached", False)),
            protected_write_attempts=int(
                (previous_attempt or {}).get("protected_write_attempts", 0)
            ),
            estimated_cost_usd=0.0, rounds=[], requested_runs=runs,
            backend_models=list(
                (previous_attempt or {}).get("backend_models", [])
            ),
        )
        attempt["rounds"] = []
        active = _package_finding(latest_findings, package)
        direct_version = _requirement_version(work, package)
        stop_reason = ""
        final_tests_passed = latest_tests_passed
        final_audit_succeeded = latest_audit_succeeded
        final_remaining: list[str] = []
        agent_statuses: list[str] = []
        all_agent_runs: list[dict[str, Any]] = []
        previous_runs = (previous_attempt or {}).get("runs", [])
        prior_trace_files = (previous_attempt or {}).get("trace_files", [])
        trace_files = (
            [str(path) for path in prior_trace_files]
            if isinstance(prior_trace_files, list) else []
        )
        completed_runs = {
            (int(item.get("round", 0)), int(item.get("run", 0))): item
            for item in previous_runs
            if item.get("completed") is True
        } if isinstance(previous_runs, list) else {}
        attempt["completed"] = True
        attempt["final_version"] = (
            latest_versions.get(package_key)
            or (active.get("version") if active else None)
            or direct_version or original_finding.get("version")
        )

        if active is None:
            parent = resolved_by.get(package_key)
            final_version = latest_versions.get(package_key)
            baseline_version = original_finding.get("version")
            changed = bool(
                final_version and baseline_version
                and _version_key(final_version) != _version_key(baseline_version)
            )
            if direct_version is None:
                attempt["result"] = (
                    "resolved_by_parent_bump"
                    if parent and changed else "unchanged_transitive"
                )
            else:
                attempt["result"] = "safe_upgrade"
            if parent and changed:
                attempt["resolved_by_parent"] = parent
            stop_reason = "clean"
        elif direct_version is None:
            final_remaining = list(dict.fromkeys(active.get("vuln_ids", [])))
            attempt["final_version"] = (
                latest_versions.get(package_key) or active.get("version")
                or original_finding.get("version")
            )
            attempt["result"] = "still_vulnerable"
            stop_reason = (
                "transitive_dependency_skipped" if active.get("fix_version")
                else "no_fix_available"
            )
        else:
            for round_number in range(1, MAX_ROUNDS + 1):
                active = _package_finding(latest_findings, package)
                if active is None:
                    stop_reason = "clean"
                    break
                from_version = (
                    latest_versions.get(package_key) or active.get("version")
                    or direct_version
                )
                to_version = active.get("fix_version")
                final_remaining = list(dict.fromkeys(active.get("vuln_ids", [])))
                attempt["final_version"] = from_version
                if not to_version or not _newer_version(to_version, from_version):
                    stop_reason = "no_fix_available"
                    attempt["result"] = "no_fix_available"
                    break

                round_info: dict[str, Any] = {
                    "round": round_number, "from": from_version,
                    "to": to_version, "tests_passed": False,
                    "agent_status": None, "steps": 0,
                    "vulns_remaining": list(final_remaining),
                }
                round_tmp: Path | None = None
                try:
                    round_tmp, round_work, round_py = make_sandbox(work)
                    if not bump_requirement(round_work, package, to_version):
                        stop_reason = "transitive_dependency_skipped"
                        attempt["result"] = stop_reason
                        break
                    installed, install_log = install(round_py, round_work)
                    if not installed:
                        _copy_repository(round_work, work)
                        attempt["log"] = install_log
                        round_info["agent_status"] = None
                        attempt["rounds"].append(round_info)
                        final_tests_passed = False
                        stop_reason = "install_failed"
                        attempt["result"] = stop_reason
                        attempt["final_version"] = to_version
                        break

                    round_versions = installed_versions(round_py, round_work)
                    latest_versions = round_versions
                    tests_passed, test_log = run_tests(round_py, round_work)
                    round_budget_stopped = False
                    budget_stopped_run: int | None = None
                    tests_passed, test_log, round_budget_stopped, budget_stopped_run, run_results = repair_broken_round(
                            round_tmp, round_work, round_py, round_number, runs,
                            completed_runs, backend, package, from_version,
                            to_version, test_log, round_info, attempt, trace_files,
                            all_agent_runs, agent_statuses, tests_passed,
                        )
                    round_info["tests_passed"] = tests_passed

                    round_findings, round_audit_succeeded = audit_result(
                        round_py, round_work,
                    )
                    if not round_audit_succeeded:
                        round_info["vulns_remaining"] = list(final_remaining)
                        attempt["rounds"].append(round_info)
                        _copy_repository(round_work, work)
                        final_tests_passed = tests_passed
                        final_audit_succeeded = False
                        stop_reason = "audit_failed"
                        attempt["result"] = stop_reason
                        break

                    remaining_finding = _package_finding(round_findings, package)
                    final_remaining = list(dict.fromkeys(
                        remaining_finding.get("vuln_ids", [])
                        if remaining_finding else []
                    ))
                    round_info["vulns_remaining"] = list(final_remaining)
                    round_info["steps"] = sum(
                        item.get("steps_used", 0) for item in run_results
                    ) if not tests_passed or round_info["agent_status"] else 0
                    attempt["rounds"].append(round_info)
                    _copy_repository(round_work, work)

                    for other in findings:
                        other_key = _package_key(other["name"])
                        final_version = round_versions.get(other_key)
                        baseline_version = other.get("version")
                        if (other_key not in direct_packages
                                and _package_finding(round_findings, other["name"]) is None
                                and final_version and baseline_version
                                and _version_key(final_version) != _version_key(baseline_version)):
                            resolved_by.setdefault(other_key, package)
                    latest_findings = round_findings
                    latest_tests_passed = tests_passed
                    latest_audit_succeeded = True
                    final_tests_passed = tests_passed
                    final_audit_succeeded = True
                    attempt["final_version"] = (
                        round_versions.get(package_key)
                        or _requirement_version(work, package)
                        or (remaining_finding or {}).get("version", to_version)
                    )

                    if round_budget_stopped:
                        budget_stopped = True
                        attempt["completed"] = False
                        stop_reason = "budget_stopped"
                        attempt["result"] = "budget_stopped"
                        attempt["not_run_cases"] = [
                            {
                                "package": package,
                                "runs": [budget_stopped_run] if budget_stopped_run else [],
                            },
                            *[
                            {
                                "package": item["name"],
                                "runs": list(range(1, runs + 1)),
                            }
                            for item in ordered_findings[finding_index + 1:]
                            ],
                        ]
                        break
                    if not tests_passed:
                        stop_reason = "agent_failed"
                        attempt["result"] = stop_reason
                        break
                    if not remaining_finding:
                        stop_reason = "clean"
                        break
                    current_version = (
                        _requirement_version(work, package)
                        or remaining_finding["version"]
                    )
                    if not _newer_version(
                        remaining_finding.get("fix_version"), current_version,
                    ):
                        stop_reason = "no_fix_available"
                        attempt["result"] = stop_reason
                        break
                    if round_number == MAX_ROUNDS:
                        stop_reason = "max_rounds"
                        attempt["result"] = stop_reason
                        break
                finally:
                    if round_tmp is not None:
                        shutil.rmtree(round_tmp, ignore_errors=True)

        finalize_attempt(
            attempt, stop_reason, final_remaining, final_audit_succeeded,
            final_tests_passed, agent_statuses, all_agent_runs, budget_stopped,
            trace_files, original, work, direct_version, started, package,
            original_finding, report, scoring_metadata,
        )
        if budget_stopped:
            break
