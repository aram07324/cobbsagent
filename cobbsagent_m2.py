"""Compatibility facade for the modular milestone-two pipeline."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent import (
    BackendChain, CachedBackend, DailyRequestCounter, GeminiBackend, OllamaBackend,
    ResponseCache, Trace, fallback_reason, run_agent, safe_path,
)
from cobbsagent import (
    _version_key, audit_result, bump_requirement, install, installed_versions,
    make_sandbox, run_tests,
)
from cobbs.m2_reporting import save_report, results_markdown, save_results_markdown
from cobbs.m2_diff import (
    _read_text_files, _repository_file_paths, _unified_text_diff, save_diff,
)
from cobbs.m2_metadata import (
    _backend_for_attempt, _backend_requests_today, _backend_total_requests,
    _is_test_python, _load_scoring_metadata, _module_candidates, _package_key,
    _repo_key, _requirement_names, _run_hidden_test, _scoring_metadata_for_package,
    _trace_write_events,
)
from cobbs.m2_tampering import _tampering_suspected
from cobbs.m2_resume import (
    _apply_resume_snapshot, _copy_repository,
    _editable_resume_path, _newer_version, _package_finding, _requirement_version,
    _resume_command, _resume_snapshot, _resume_state, discover_repositories,
)
from cobbs.m2_pipeline import (
    INPUT_PRICE_PER_MILLION, MAX_ROUNDS, OUTPUT_PRICE_PER_MILLION,
)
import cobbs.m2_cli as _cli
import cobbs.m2_diff as _diff
import cobbs.m2_metadata as _metadata
import cobbs.m2_attempts as _attempts
import cobbs.m2_finalize as _finalize
import cobbs.m2_pipeline as _pipeline
import cobbs.m2_repair as _repair
import cobbs.m2_scoring as _scoring

def _apply_scoring(
    attempt: dict[str, Any], original: Path, work: Path,
    metadata: dict[str, Any] | None, tests_passed: bool, trace_files: list[str],
) -> None:
    _scoring._run_hidden_test = _run_hidden_test
    _scoring._tampering_suspected = _tampering_suspected
    _scoring._read_text_files = _read_text_files
    _scoring._repository_file_paths = _repository_file_paths
    _scoring._unified_text_diff = _unified_text_diff
    _scoring._trace_write_events = _trace_write_events
    _scoring._module_candidates = _module_candidates
    _scoring._requirement_names = _requirement_names
    _scoring._apply_scoring(
        attempt, original, work, metadata, tests_passed, trace_files,
    )

def record_proof(
    attempt: dict[str, Any], py: Path, work: Path, package: str, tests_passed: bool,
) -> None:
    """Record the legacy package-specific audit proof."""
    _diff.audit_result = audit_result
    _diff.record_proof(attempt, py, work, package, tests_passed)

def _sync_pipeline() -> None:
    names = (
        "audit_result", "_version_key", "bump_requirement", "install",
        "installed_versions", "make_sandbox", "run_tests", "run_agent", "Trace",
        "BackendChain", "CachedBackend", "GeminiBackend", "fallback_reason",
        "save_diff", "_load_scoring_metadata", "_package_key",
        "_repo_key", "_scoring_metadata_for_package", "_backend_for_attempt",
        "_backend_total_requests", "_backend_requests_today", "_copy_repository",
        "_newer_version", "_package_finding", "_requirement_version",
        "_resume_state", "_resume_snapshot", "_apply_resume_snapshot", "MAX_ROUNDS",
        "INPUT_PRICE_PER_MILLION", "OUTPUT_PRICE_PER_MILLION",
        "save_results_markdown", "save_report",
    )
    for name in names:
        setattr(_pipeline, name, globals()[name])
    attempt_names = (
        "audit_result", "BackendChain", "save_diff", "_backend_for_attempt",
        "_backend_requests_today", "_backend_total_requests", "_package_key",
        "_repo_key", "_scoring_metadata_for_package", "_copy_repository",
        "_newer_version", "_package_finding", "_requirement_version",
        "_resume_snapshot", "_resume_state", "_apply_resume_snapshot",
        "install", "make_sandbox", "run_tests", "Trace",
        "_version_key", "bump_requirement", "installed_versions",
        "fallback_reason", "run_agent", "MAX_ROUNDS",
        "INPUT_PRICE_PER_MILLION", "OUTPUT_PRICE_PER_MILLION",
    )
    for name in attempt_names:
        value = globals()[name]
        setattr(_attempts, name, value)
        if hasattr(_repair, name):
            setattr(_repair, name, value)
    _finalize.save_diff = save_diff
    _finalize._apply_scoring = _apply_scoring
    _finalize._scoring_metadata_for_package = _scoring_metadata_for_package
    _finalize._resume_state = _resume_state
    _finalize.INPUT_PRICE_PER_MILLION = INPUT_PRICE_PER_MILLION
    _finalize.OUTPUT_PRICE_PER_MILLION = OUTPUT_PRICE_PER_MILLION

def run_repository(
    original: Path, backend: GeminiBackend, runs: int = 1,
    resume_attempts: list[dict[str, Any]] | None = None,
    scoring_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one repository through the modular milestone-two pipeline."""
    _sync_pipeline()
    return _pipeline.run_repository(
        original, backend, runs, resume_attempts, scoring_metadata,
    )

def parse_backends(value: str) -> list[tuple[str, str]]:
    """Parse backend specifications without changing the model names."""
    return _cli.parse_backends(value)

def create_backend_chain(
    value: str, max_requests: int, cache: ResponseCache | None,
    cache_only: bool = False,
) -> BackendChain:
    """Build the CLI backend chain with legacy monkeypatch points applied."""
    _sync_cli()
    return _cli.create_backend_chain(value, max_requests, cache, cache_only)

def _sync_cli() -> None:
    names = (
        "DailyRequestCounter", "BackendChain", "ResponseCache", "GeminiBackend",
        "OllamaBackend", "run_repository", "save_report", "save_results_markdown",
        "_load_scoring_metadata", "_package_key", "_repo_key",
        "_backend_requests_today", "_resume_command", "discover_repositories",
    )
    for name in names:
        setattr(_cli, name, globals()[name])

def main(argv: list[str] | None = None) -> None:
    """Run the milestone-two CLI through its modular implementation."""
    _sync_cli()
    _cli.main(argv)

if __name__ == "__main__":
    main()
