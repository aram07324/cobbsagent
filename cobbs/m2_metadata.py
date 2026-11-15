"""Evaluation metadata and sandbox-only hidden-test helpers."""

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .cache import BackendChain, CachedBackend
from .safety import safe_path
from .sandbox import install, make_sandbox

_SCORING_FIELDS = {"package", "category", "agent_expected", "canary", "hidden_test", "truth"}

def _package_key(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()

def _backend_total_requests(backend: Any) -> int:
    gemini_total = getattr(backend, "gemini_total_requests", None)
    if gemini_total is not None:
        return int(gemini_total)
    if getattr(backend, "backend_name", "") == "ollama":
        return 0
    return int(getattr(backend, "total_requests", 0))

def _backend_requests_today(backend: Any) -> int:
    return int(getattr(backend, "requests_today", 0))

def _backend_for_attempt(backend: Any, run_index: int) -> CachedBackend:
    if isinstance(backend, BackendChain):
        return backend.for_attempt(run_index)
    return CachedBackend(backend, None, run_index)

def _repo_key(value: str) -> str:
    return str(Path(value).resolve()).casefold()

def _load_scoring_metadata(original: Path) -> dict[str, Any] | None:
    project = Path(__file__).resolve().parent.parent
    eval_root = (project / "eval").resolve()
    try:
        original.resolve().relative_to(eval_root)
    except ValueError:
        return None
    expected_file = eval_root / "expected.json"
    if not expected_file.is_file():
        return None
    try:
        expected = json.loads(expected_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not load evaluation metadata: {exc}") from exc
    if not isinstance(expected, dict):
        raise RuntimeError("Evaluation metadata must be a JSON object")
    entry = next(
        (value for name, value in expected.items()
         if isinstance(name, str) and name.casefold() == original.name.casefold()),
        None,
    )
    if not isinstance(entry, dict):
        return None
    return {key: entry[key] for key in _SCORING_FIELDS if key in entry}

def _trace_write_events(trace_files: list[str]) -> list[tuple[str, str, bool]]:
    events: list[tuple[str, str, bool]] = []
    for trace_file in trace_files:
        path = Path(trace_file)
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if row.get("event") == "agent_write" and isinstance(row.get("path"), str):
                events.append((row["path"], "", True))
            elif (
                row.get("event") == "agent_write_attempt"
                and isinstance(row.get("path"), str)
                and isinstance(row.get("content"), str)
            ):
                events.append((row["path"], row["content"], False))
    return events

def _scoring_metadata_for_package(
    metadata: dict[str, Any] | None, package: str,
) -> dict[str, Any] | None:
    if metadata is None:
        return None
    expected_package = metadata.get("package")
    if (
        isinstance(expected_package, str)
        and _package_key(expected_package) != _package_key(package)
    ):
        return None
    return metadata

def _run_hidden_test(work: Path, hidden_test: str) -> tuple[bool, str]:
    project = Path(__file__).resolve().parent.parent
    hidden_root = project / "eval_hidden"
    source = safe_path(hidden_root, hidden_test)
    if not source.is_file():
        raise RuntimeError(f"Hidden test does not exist: {hidden_test}")
    tmp, hidden_work, py = make_sandbox(work)
    try:
        try:
            installed, install_log = install(py, hidden_work)
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"Could not install hidden-test environment: {exc}"
        if not installed:
            return False, install_log
        with tempfile.TemporaryDirectory(prefix="cobbsagent_hidden_") as staging:
            staged_test = Path(staging) / source.name
            shutil.copyfile(source, staged_test)
            env = os.environ.copy()
            env["PYTHONPATH"] = str(hidden_work) + (
                os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
            )
            try:
                result = subprocess.run(
                    [str(py), "-m", "pytest", "-q", str(staged_test)],
                    cwd=hidden_work, env=env, capture_output=True, text=True,
                    timeout=120,
                )
            except subprocess.TimeoutExpired:
                return False, "Hidden test timed out after 120 seconds."
            except OSError as exc:
                return False, f"Could not run hidden test: {exc}"
            return result.returncode == 0, (result.stdout + result.stderr)[-3000:]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

def _module_candidates(name: str) -> set[str]:
    normalized = re.sub(r"[^a-z0-9]", "", name.casefold())
    aliases = {
        "pyjwt": "jwt",
        "pyyaml": "yaml",
        "beautifulsoup4": "bs4",
        "pillow": "pil",
    }
    return {normalized, *([aliases[normalized]] if normalized in aliases else [])}

def _requirement_names(work: Path) -> set[str]:
    requirements = work / "requirements.txt"
    if not requirements.is_file():
        return set()
    names: set[str] = set()
    for line in requirements.read_text(encoding="utf-8").splitlines():
        match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9_.-]*)", line)
        if match and not line.lstrip().startswith("#"):
            names.add(match.group(1))
    return names

def _is_test_python(relative: str) -> bool:
    parts = relative.casefold().split("/")
    name = parts[-1]
    return (
        any(part in {"test", "tests"} or part.startswith("tests_") for part in parts)
        or name in {"test.py", "conftest.py"}
        or name.startswith(("test_", "test-")) or name.endswith("_test.py")
    )
