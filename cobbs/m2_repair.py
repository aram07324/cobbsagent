"""Repair broken tests using isolated, independently verified attempts."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .cache import BackendChain
from .m2_metadata import _backend_for_attempt, _backend_total_requests
from .m2_resume import _apply_resume_snapshot, _copy_repository, _resume_snapshot
from .sandbox import run_tests
from .tracing import Trace
from .agent_loop import fallback_reason, run_agent

def repair_broken_round(
    round_tmp: Path, round_work: Path, round_py: Path, round_number: int,
    runs: int, completed_runs: dict[tuple[int, int], dict[str, Any]], backend: Any,
    package: str, from_version: str, to_version: str, test_log: str,
    round_info: dict[str, Any], attempt: dict[str, Any], trace_files: list[str],
    all_agent_runs: list[dict[str, Any]], agent_statuses: list[str],
    tests_passed: bool,
) -> tuple[bool, str, bool, int | None, list[dict[str, Any]]]:
    """Run isolated repair attempts and independently retest each reported fix."""
    run_results: list[dict[str, Any]] = []
    round_budget_stopped = False
    budget_stopped_run: int | None = None
    if not tests_passed:
        run_results: list[dict[str, Any]] = []
        successful_work: Path | None = None
        last_agent_work: Path | None = None
        for run_number in range(1, runs + 1):
            cached = completed_runs.get((round_number, run_number))
            if cached is not None:
                agent_work = round_tmp / (
                    f"agent-round-{round_number}-{run_number}-cached"
                )
                last_agent_work = agent_work
                _copy_repository(round_work, agent_work)
                run_result = dict(cached)
                _apply_resume_snapshot(
                    agent_work, run_result.get("resume_files"),
                )
                if run_result.get("result") == "agent_fixed":
                    verified, verify_log = run_tests(round_py, agent_work)
                    run_result["verification_log"] = verify_log
                    if verified:
                        successful_work = successful_work or agent_work
                    else:
                        run_result["result"] = "agent_failed"
                run_results.append(run_result)
                continue

            while True:
                agent_backend = _backend_for_attempt(
                    backend, run_number,
                )
                backend_index = getattr(backend, "index", 0)
                backend_suffix = re.sub(
                    r"[^A-Za-z0-9._-]+", "_",
                    f"{agent_backend.backend_name}-{agent_backend.model}",
                )
                work_suffix = (
                    f"-{backend_index}-{backend_suffix}"
                    if isinstance(backend, BackendChain) else ""
                )
                agent_work = round_tmp / (
                    f"agent-round-{round_number}-{run_number}{work_suffix}"
                )
                last_agent_work = agent_work
                _copy_repository(round_work, agent_work)
                request_start = _backend_total_requests(backend)
                run_result: dict[str, Any] = {
                    "round": round_number, "run": run_number,
                    "backend": agent_backend.backend_name,
                    "model": agent_backend.model,
                }
                trace = Trace()
                trace_path = getattr(trace, "path", None)
                if trace_path is not None:
                    trace_name = str(trace_path)
                    trace_files.append(trace_name)
                    run_result["trace_file"] = trace_name
                try:
                    outcome = run_agent(
                        agent_backend, agent_work, round_py, package,
                        from_version, to_version, test_log, trace,
                    )
                except Exception as exc:
                    request_delta = max(
                        0, _backend_total_requests(backend) - request_start,
                    )
                    reason = fallback_reason(exc)
                    run_result.update(
                        result="agent_error", error=str(exc),
                        steps_used=getattr(exc, "steps_used", 0),
                        input_tokens=getattr(exc, "input_tokens", 0),
                        output_tokens=getattr(exc, "output_tokens", 0),
                        requests_used=request_delta,
                        agent_requests=agent_backend.agent_requests,
                        cache_hits=agent_backend.cache_hits,
                        cached=agent_backend.cache_hits > 0,
                        protected_write_attempts=getattr(
                            exc, "protected_write_attempts", 0,
                        ),
                        completed=True,
                    )
                    can_fallback = (
                        reason is not None
                        and isinstance(backend, BackendChain)
                        and backend.advance()
                    )
                    if can_fallback:
                        run_result["result"] = "backend_fallback"
                        run_result["fallback_reason"] = reason
                    elif reason == "daily_budget":
                        run_result["result"] = "budget_stopped"
                        run_result["completed"] = False
                        round_budget_stopped = True
                        budget_stopped_run = run_number
                    else:
                        run_result["resume_files"] = _resume_snapshot(
                            round_work, agent_work,
                        )
                else:
                    request_delta = max(
                        0, _backend_total_requests(backend) - request_start,
                    )
                    run_result.update(
                        result="agent_failed",
                        status=outcome.get("status"),
                        summary=outcome.get("summary", ""),
                        steps_used=outcome.get("steps_used", 0),
                        input_tokens=outcome.get("input_tokens", 0),
                        output_tokens=outcome.get("output_tokens", 0),
                        requests_used=request_delta,
                        agent_requests=agent_backend.agent_requests,
                        cache_hits=agent_backend.cache_hits,
                        cached=agent_backend.cache_hits > 0,
                        protected_write_attempts=outcome.get(
                            "protected_write_attempts", 0,
                        ),
                        completed=True,
                    )
                    if outcome.get("status") == "fixed":
                        verified, verify_log = run_tests(
                            round_py, agent_work,
                        )
                        run_result.update(
                            result=(
                                "agent_fixed" if verified
                                else "agent_failed"
                            ),
                            verification_log=verify_log,
                        )
                        if verified and successful_work is None:
                            successful_work = agent_work
                    run_result["resume_files"] = _resume_snapshot(
                        round_work, agent_work,
                    )
                run_results.append(run_result)
                attempt["steps_used"] += run_result.get(
                    "steps_used", 0,
                )
                attempt["input_tokens"] += run_result.get(
                    "input_tokens", 0,
                )
                attempt["output_tokens"] += run_result.get(
                    "output_tokens", 0,
                )
                attempt["requests_used"] += run_result.get(
                    "requests_used", 0,
                )
                attempt["agent_requests"] += run_result.get(
                    "agent_requests", 0,
                )
                attempt["cache_hits"] += run_result.get(
                    "cache_hits", 0,
                )
                attempt["cached"] = (
                    attempt["cached"] or run_result.get("cached", False)
                )
                attempt["protected_write_attempts"] += (
                    run_result.get("protected_write_attempts", 0)
                )
                pair = {
                    "backend": agent_backend.backend_name,
                    "model": agent_backend.model,
                }
                if pair not in attempt["backend_models"]:
                    attempt["backend_models"].append(pair)
                if run_result["result"] == "backend_fallback":
                    continue
                if round_budget_stopped:
                    break
                break
            if round_budget_stopped:
                break

        all_agent_runs.extend(run_results)
        selected_work = successful_work or last_agent_work
        if selected_work is not None:
            _copy_repository(selected_work, round_work)
        tests_passed, test_log = run_tests(round_py, round_work)
        if round_budget_stopped:
            round_info["agent_status"] = "budget_stopped"
        elif successful_work is not None and tests_passed:
            round_info["agent_status"] = "fixed"
        elif run_results and all(
            item["result"] == "agent_error" for item in run_results
        ):
            round_info["agent_status"] = "error"
        else:
            round_info["agent_status"] = "gave_up"
        agent_statuses.append(round_info["agent_status"])
        attempt["log"] = test_log
    return tests_passed, test_log, round_budget_stopped, budget_stopped_run, run_results
