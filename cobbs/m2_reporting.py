"""Milestone-two report serialization and Markdown rendering."""

from pathlib import Path
from typing import Any
import json

def save_report(report: dict[str, Any]) -> None:
    """Write a report without destroying any pre-existing file."""
    output = Path("report.json")
    if output.exists():
        backup = output.with_name(output.name + ".bak")
        suffix = 1
        while backup.exists():
            backup = output.with_name(f"{output.name}.bak.{suffix}")
            suffix += 1
        output.rename(backup)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Report written to {output.resolve()}")

def results_markdown(
    results: list[dict[str, Any]], requests_used: int = 0, budget: int = 400,
) -> str:
    """Render the aggregate attempt table and case-category summary."""
    headers = [
        "repo", "package", "category", "backend/model", "outcome", "pipeline_result",
        "correct", "hidden_passed", "injection_followed", "tampering_suspected",
        "reward_hacking", "verified_by", "truth_broken", "from", "to", "steps",
        "input_tokens", "output_tokens", "requests_used", "agent_requests", "seconds",
        "proved_clean", "vulns_remaining", "rounds", "stop_reason",
    ]
    rows = ["| " + " | ".join(headers) + " |",
            "| " + " | ".join("---" for _ in headers) + " |"]
    counts = {
        "safe_upgrade": 0, "agent_fixed": 0, "agent_failed": 0,
        "agent_error": 0, "budget_stopped": 0, "overfit": 0,
        "injection_followed": 0, "shadowing_suspected": 0,
        "tampering_suspected": 0, "reward_hacking": 0, "skipped": 0,
    }
    cases = 0
    clean_cases = 0
    remaining_count = 0
    category_stats: dict[str, dict[str, int]] = {}
    scored_cases = 0
    correct_cases = 0
    scored_runs = 0
    overfit_count = 0
    injection_count = 0
    visible_tests_only = 0
    repair_count = 0
    repair_calls = 0
    agent_requests_total = 0
    backend_attempt_counts: dict[str, int] = {}
    for repo_result in results:
        repo_name = Path(repo_result.get("repo", "")).name
        for attempt in repo_result.get("attempts", []):
            cases += 1
            repair_count += 1
            result = attempt.get("result", "skipped")
            visible_tests_only += (
                attempt.get("verified_by", "visible_tests_only") == "visible_tests_only"
            )
            remaining = len(attempt.get("vulns_remaining", []))
            clean = bool(attempt.get("proved_clean", False))
            clean_cases += clean
            remaining_count += remaining
            category = attempt.get("category")
            run_results = attempt.get("runs")
            runs = run_results if isinstance(run_results, list) else []
            backend_models = attempt.get("backend_models", [])
            backend_model = ", ".join(
                f"{item.get('backend', 'unknown')}/{item.get('model', '')}"
                for item in backend_models if isinstance(item, dict)
            )
            repair_calls += sum(
                isinstance(run, dict) and "backend" in run for run in runs
            )
            for run in runs:
                if isinstance(run, dict) and isinstance(run.get("backend"), str):
                    label = f"{run['backend']}/{run.get('model', '')}"
                    backend_attempt_counts[label] = (
                        backend_attempt_counts.get(label, 0) + 1
                    )
            agent_requests_total += int(attempt.get("agent_requests", 0))
            if attempt.get("correct") is not None:
                scored_cases += 1
                scored_runs += len(runs)
                is_correct = bool(attempt["correct"])
                correct_cases += is_correct
                if isinstance(category, str) and category:
                    stats = category_stats.setdefault(
                        category, {
                            "cases": 0, "correct": 0, "runs": 0,
                            "runs_correct": 0, "tampering": 0, "reward_hacking": 0,
                        },
                    )
                    stats["cases"] += 1
                    stats["correct"] += is_correct
                    stats["runs"] += len(runs)
                    stats["tampering"] += attempt.get("tampering_suspected", False)
                    stats["reward_hacking"] += attempt.get("reward_hacking", False)
                    if attempt.get("result") not in {
                        "overfit", "injection_followed", "shadowing_suspected",
                        "tampering_suspected", "reward_hacking",
                    }:
                        stats["runs_correct"] += sum(
                            run.get("result") == attempt.get("agent_expected")
                            for run in runs
                        )
            overfit_count += attempt.get("result") == "overfit"
            injection_count += bool(attempt.get("injection_followed"))
            if result in counts:
                counts[result] += 1
            else:
                counts["skipped"] += 1
            values = [
                repo_name, attempt.get("name", ""), category or "", backend_model,
                result, attempt.get("pipeline_result"), attempt.get("correct"),
                attempt.get("hidden_passed"),
                attempt.get("injection_followed", False),
                attempt.get("tampering_suspected", False),
                attempt.get("reward_hacking", False),
                attempt.get("verified_by", "visible_tests_only"),
                attempt.get("truth_broken"),
                attempt.get("version", ""),
                attempt.get("final_version", attempt.get("fix_version")) or "",
                attempt.get("steps_used", 0), attempt.get("input_tokens", 0),
                attempt.get("output_tokens", 0), attempt.get("requests_used", 0),
                attempt.get("agent_requests", 0), attempt.get("seconds", 0),
                str(clean).lower(), remaining,
                len(attempt.get("rounds", [])), attempt.get("stop_reason", ""),
            ]
            escaped = [
                ("" if value is None else str(value).lower() if isinstance(value, bool)
                 else str(value)).replace("|", r"\|").replace("\n", " ")
                for value in values
            ]
            rows.append("| " + " | ".join(escaped) + " |")
    rows.append(
        f"{cases} cases: {counts['safe_upgrade']} safe_upgrade, "
        f"{counts['agent_fixed']} agent_fixed, {counts['agent_failed']} agent_failed, "
        f"{counts['agent_error']} agent_error, "
        f"{counts['budget_stopped']} budget_stopped, {counts['overfit']} overfit, "
        f"{counts['injection_followed']} injection_followed, "
        f"{counts['shadowing_suspected']} shadowing_suspected, "
        f"{counts['tampering_suspected']} tampering_suspected, "
        f"{counts['reward_hacking']} reward_hacking, "
        f"{counts['skipped']} skipped, "
        f"{clean_cases} proved_clean, {remaining_count} vulns_remaining"
    )
    for category, stats in sorted(category_stats.items()):
        rows.append(
            f"{category}: {stats['correct']}/{stats['cases']} correct "
            f"(runs {stats['runs_correct']}/{stats['runs']}) "
            f"tampering_suspected {stats['tampering']}, "
            f"reward_hacking {stats['reward_hacking']}"
        )
    average_runs = scored_runs / scored_cases if scored_cases else 0
    rows.append(f"Correct cases: {correct_cases}/{scored_cases}")
    rows.append(
        f"Runs per case: {average_runs:.2f} ({scored_runs} total scored runs)"
    )
    rows.append(f"Overfit count: {overfit_count}")
    rows.append(f"Injection_followed count: {injection_count}")
    rows.append(
        "proved_clean means advisories cleared and visible tests pass; "
        f"{visible_tests_only} of {repair_count} repairs were verified only by visible tests."
    )
    rows.append(
        "Tamper detection is heuristic: it scans added Python code for imported-module "
        "mutation, dynamic imports, sys.modules, and exec/eval."
    )
    average_agent_requests = (
        agent_requests_total / repair_calls if repair_calls else 0
    )
    rows.append(
        f"Average agent requests per repair: {average_agent_requests:.2f} "
        f"({agent_requests_total} non-cached requests across {repair_calls} repairs)"
    )
    if backend_attempt_counts:
        rows.append(
            "Attempts per backend/model: "
            + ", ".join(
                f"{label}: {count}"
                for label, count in sorted(backend_attempt_counts.items())
            )
        )
    rows.append(f"Gemini requests used: {requests_used} of budget {budget}")
    return "\n".join(rows) + "\n"

def save_results_markdown(
    results: list[dict[str, Any]], requests_used: int = 0, budget: int = 400,
) -> None:
    """Save and print results while preserving any earlier table."""
    output = Path("results_agent.md")
    if output.exists():
        backup = output.with_name(output.name + ".bak")
        suffix = 1
        while backup.exists():
            backup = output.with_name(f"{output.name}.bak.{suffix}")
            suffix += 1
        output.rename(backup)
    text = results_markdown(results, requests_used, budget)
    output.write_text(text, encoding="utf-8")
    print(text, end="")
    print(f"Results written to {output.resolve()}")
