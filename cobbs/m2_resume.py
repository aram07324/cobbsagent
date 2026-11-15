"""Resume snapshots and dependency-resolution helpers."""

import re
import shutil
import subprocess
import argparse
from pathlib import Path
from typing import Any

from .safety import safe_path
from .m2_diff import _read_text_files
from .m2_metadata import _package_key
from .versions import _version_key

def _resume_command(args: argparse.Namespace) -> str:
    command = ["python", "cobbsagent_m2.py", "--resume"]
    command.extend(["--runs", str(args.runs), "--max-requests", str(args.max_requests)])
    command.extend(["--backends", args.backends])
    if args.no_cache:
        command.append("--no-cache")
    if args.cache_only:
        command.append("--cache-only")
    command.extend(str(path) for path in args.paths)
    return subprocess.list2cmdline(command)

def _editable_resume_path(relative: str) -> bool:
    parts = relative.replace("\\", "/").split("/")
    name = parts[-1].casefold()
    return not (
        name == "requirements.txt"
        or "tests" in {part.casefold() for part in parts}
        or (name.startswith("test") and name.endswith(".py"))
        or name.endswith("_test.py")
        or name == "conftest.py"
    )

def _resume_snapshot(before: Path, after: Path) -> dict[str, str]:
    old_files = _read_text_files(before)
    new_files = _read_text_files(after)
    return {
        path: content
        for path, content in new_files.items()
        if _editable_resume_path(path) and old_files.get(path) != content
    }

def _resume_state(before: Path, after: Path) -> dict[str, str]:
    old_files = _read_text_files(before)
    new_files = _read_text_files(after)
    return {
        path: content
        for path, content in new_files.items()
        if old_files.get(path) != content
        and (
            path.rsplit("/", 1)[-1].casefold() == "requirements.txt"
            or _editable_resume_path(path)
        )
    }

def _apply_resume_snapshot(
    work: Path, snapshot: Any, include_requirements: bool = False,
) -> bool:
    if not isinstance(snapshot, dict):
        return False
    applied = False
    for relative, content in snapshot.items():
        if not isinstance(relative, str) or not isinstance(content, str):
            continue
        if (not _editable_resume_path(relative)
                and not (include_requirements
                         and relative.rsplit("/", 1)[-1].casefold() == "requirements.txt")):
            continue
        target = safe_path(work, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        applied = True
    return applied


def restore_completed_attempt(
    previous_attempt: dict[str, Any] | None, runs: int, report: dict[str, Any],
    work: Path, findings: list[dict[str, Any]], direct_packages: set[str],
    package_key: str, package: str, latest_versions: dict[str, str],
    latest_tests_passed: bool, latest_findings: list[dict[str, Any]],
    latest_audit_succeeded: bool, resolved_by: dict[str, str],
    make_sandbox_fn: Any, install_fn: Any, installed_versions_fn: Any,
    run_tests_fn: Any, audit_result_fn: Any,
) -> tuple[bool, dict[str, str], bool, list[dict[str, Any]], bool]:
    """Restore a completed attempt and refresh its resolved dependency state."""
    completed = bool(
        previous_attempt
        and previous_attempt.get(
            "completed", previous_attempt.get("result") != "budget_stopped",
        )
        and previous_attempt.get("requested_runs", runs) == runs
        and isinstance(previous_attempt.get("resume_state"), dict)
    )
    if not completed:
        return (
            False, latest_versions, latest_tests_passed,
            latest_findings, latest_audit_succeeded,
        )

    state = previous_attempt["resume_state"]
    _apply_resume_snapshot(work, state, include_requirements=True)
    if state:
        refresh_tmp, refresh_work, refresh_py = make_sandbox_fn(work)
        try:
            refreshed, refresh_log = install_fn(refresh_py, refresh_work)
            if not refreshed:
                raise RuntimeError(
                    f"Could not restore completed resume state: {refresh_log}"
                )
            latest_versions = installed_versions_fn(refresh_py, refresh_work)
            latest_tests_passed, _ = run_tests_fn(refresh_py, refresh_work)
            latest_findings, latest_audit_succeeded = audit_result_fn(
                refresh_py, refresh_work,
            )
            if not latest_audit_succeeded:
                raise RuntimeError(
                    "pip-audit returned invalid JSON while resuming"
                )
            for other in findings:
                other_key = _package_key(other["name"])
                final_version = latest_versions.get(other_key)
                baseline_version = other.get("version")
                if (
                    other_key != package_key
                    and other_key not in direct_packages
                    and _package_finding(latest_findings, other["name"]) is None
                    and final_version and baseline_version
                    and _version_key(final_version) != _version_key(baseline_version)
                ):
                    resolved_by.setdefault(other_key, package)
        finally:
            shutil.rmtree(refresh_tmp, ignore_errors=True)
    report["attempts"].append(previous_attempt)
    return (
        True, latest_versions, latest_tests_passed,
        latest_findings, latest_audit_succeeded,
    )


def _requirement_version(work: Path, package: str) -> str | None:
    """Return a directly pinned version, or None for transitive/unpinned packages."""
    requirements = work / "requirements.txt"
    if not requirements.is_file():
        return None
    pattern = re.compile(
        r"^\s*([A-Za-z0-9][A-Za-z0-9_.-]*)(?:\[[^\]]+\])?\s*"
        r"(?:===|==|~=|!=|<=|>=|<|>)\s*([^\s;#]+)",
    )
    for line in requirements.read_text(encoding="utf-8").splitlines():
        match = pattern.match(line.split("#", 1)[0])
        if match and _package_key(match.group(1)) == _package_key(package):
            return match.group(2)
    return None

def _package_finding(
    findings: list[dict[str, Any]], package: str,
) -> dict[str, Any] | None:
    key = _package_key(package)
    return next(
        (finding for finding in findings
         if _package_key(finding.get("name", "")) == key),
        None,
    )

def _newer_version(candidate: str | None, current: str) -> bool:
    return bool(candidate and _version_key(candidate) > _version_key(current))

def _copy_repository(source: Path, destination: Path) -> None:
    """Copy source edits while excluding caches and preserving prior agent changes."""
    shutil.copytree(
        source, destination, dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(
            ".git", ".venv", "venv", "__pycache__", ".pytest_cache",
        ),
    )

def discover_repositories(paths: list[Path]) -> list[Path]:
    """Expand directories without requirements into child repositories."""
    repos: list[Path] = []
    for path in paths:
        candidate = path.resolve(strict=True)
        if not candidate.is_dir():
            raise ValueError(f"not a repository directory: {candidate}")
        if (candidate / "requirements.txt").is_file():
            repos.append(candidate)
        else:
            repos.extend(
                child.resolve() for child in sorted(candidate.iterdir())
                if child.is_dir() and (child / "requirements.txt").is_file()
            )
    return list(dict.fromkeys(repos))
