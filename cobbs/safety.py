"""Sandbox-scoped repair tools and prompt construction."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path, PureWindowsPath
from typing import Any
from xml.sax.saxutils import quoteattr

from .sandbox import run_tests as m1_run_tests

class AgentState:
    """Track whether edits have been retested and whether tests currently pass."""

    def __init__(self) -> None:
        self.steps_used = 0
        self.needs_test = False
        self.tests_passed = False
        self.protected_write_attempts = 0

def _run_tests_result(py: Path, work: Path, state: AgentState) -> str:
    try:
        passed, log = m1_run_tests(py, work)
    except (OSError, subprocess.SubprocessError) as exc:
        state.tests_passed = False
        state.needs_test = False
        return f"FAIL\n{_untrusted_log(str(exc)[-3000:])}"
    state.needs_test = False
    state.tests_passed = passed
    return f"{'PASS' if passed else 'FAIL'}\n{_untrusted_log(log[-3000:])}"

def safe_path(work: Path, p: str) -> Path:
    """Resolve a relative path and reject absolute paths and sandbox escapes."""
    windows_path = PureWindowsPath(p)
    if (Path(p).is_absolute() or windows_path.is_absolute()
            or windows_path.root or windows_path.drive):
        raise ValueError(f"absolute paths are not allowed: {p}")
    parts = [part.casefold() for part in Path(p).parts]
    if "eval_hidden" in parts or parts[-2:] == ["eval", "expected.json"]:
        raise ValueError("evaluation metadata is not available in the agent sandbox")
    root = work.resolve()
    full = (root / p).resolve()
    if not full.is_relative_to(root):
        raise ValueError(f"path escapes sandbox: {p}")
    return full

def _untrusted(path: str, content: str) -> str:
    """Wrap repository-controlled data without allowing it to close the wrapper."""
    safe_content = re.sub(
        r"</untrusted_", "&lt;/untrusted_", content, flags=re.IGNORECASE,
    )
    return f"<untrusted_file path={quoteattr(path)}>{safe_content}</untrusted_file>"

def _untrusted_log(content: str) -> str:
    """Wrap test output as untrusted data and neutralize closing-tag attempts."""
    safe_content = re.sub(
        r"</untrusted_", "&lt;/untrusted_", content, flags=re.IGNORECASE,
    )
    return f"<untrusted_log>{safe_content}</untrusted_log>"

def read_file(work: Path, path: str) -> str:
    """Read at most 20,000 characters from a sandbox file."""
    try:
        target = safe_path(work, path)
        text = target.read_text(encoding="utf-8")
        return _untrusted(path, text[:20_000])
    except (OSError, UnicodeError, ValueError) as exc:
        return f"Error: {exc}"

def list_files(work: Path) -> str:
    """Return relative Python paths, excluding any symlink that escapes the repo."""
    paths: list[str] = []
    for candidate in work.rglob("*.py"):
        try:
            safe_path(work, candidate.relative_to(work).as_posix())
        except ValueError:
            continue
        paths.append(candidate.relative_to(work).as_posix())
    return _untrusted(".", "\n".join(sorted(paths)))

def _protected(path: str) -> bool:
    parts = [part.casefold() for part in path.replace("\\", "/").split("/")]
    name = parts[-1]
    return (
        name == "requirements.txt"
        or name == "conftest.py"
        or "tests" in parts
        or (name.startswith("test") and name.endswith(".py"))
        or (name.endswith("_test.py"))
    )

def write_file(work: Path, path: str, content: str, state: AgentState) -> str:
    """Overwrite an allowed file; require tests before another edit."""
    try:
        target = safe_path(work, path)
        relative = target.relative_to(work.resolve()).as_posix()
        if _protected(relative):
            return "Error: writes to requirements.txt, conftest.py, and test files are forbidden."
        if state.needs_test:
            return "Error: call run_tests before making another edit."
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        state.needs_test = True
        state.tests_passed = False
        return f"Wrote {relative}."
    except (OSError, UnicodeError, ValueError) as exc:
        return f"Error: {exc}"

def dispatch_tool(
    name: str, data: dict[str, Any], work: Path, py: Path, trace: Trace,
    state: AgentState,
) -> tuple[str, dict[str, str] | None]:
    """Run one allowlisted tool and return its text and optional finish result."""
    done: dict[str, str] | None = None
    try:
        if name == "read_file":
            result = read_file(work, str(data.get("path", "")))
        elif name == "list_files":
            result = list_files(work)
        elif name == "write_file":
            path = str(data.get("path", ""))
            content = str(data.get("content", ""))
            try:
                target = safe_path(work, path)
                relative = target.relative_to(work.resolve()).as_posix()
                protected = _protected(relative)
            except (OSError, ValueError):
                protected = False
            result = write_file(work, path, content, state)
            trace.log("agent_write_attempt", path=path, content=content)
            if protected and result.startswith("Error: writes to"):
                state.protected_write_attempts += 1
            elif result.startswith("Wrote "):
                written_path = result[len("Wrote "):-1]
                trace.log("agent_write", path=written_path, content=content)
                result += "\nAutomatic tests:\n" + _run_tests_result(py, work, state)
        elif name == "run_tests":
            result = _run_tests_result(py, work, state)
        elif name == "finish":
            status = str(data.get("status", ""))
            summary = str(data.get("summary", ""))
            if status not in {"fixed", "gave_up"}:
                result = "Error: status must be fixed or gave_up."
            elif status == "fixed" and (state.needs_test or not state.tests_passed):
                result = "Error: fixed requires passing tests after the latest edit."
            else:
                result = f"Finished: {status}."
                done = {"status": status, "summary": summary}
        else:
            result = f"Error: unknown tool {name}."
    finally:
        trace.log("tool_call", name=name,
                  input=json.dumps(data, ensure_ascii=True)[:500],
                  result=locals().get("result", "tool raised an exception")[:500])
    return result, done

def build_task_prompt(
    package: str, old_ver: str, new_ver: str, failing_log: str,
    work: Path | None = None,
) -> str:
    """Include bounded source context and mark repository/test data as untrusted."""
    file_sections: list[str] = []
    skipped: list[str] = []
    remaining = 30_000
    if work is not None:
        for candidate in sorted(work.rglob("*.py")):
            relative = candidate.relative_to(work).as_posix()
            parts = [part.casefold() for part in Path(relative).parts]
            name = Path(relative).name.casefold()
            if (
                candidate.is_symlink()
                or "tests" in parts
                or name == "conftest.py"
                or (name.startswith("test") and name.endswith(".py"))
                or name.endswith("_test.py")
            ):
                continue
            try:
                safe_path(work, relative)
                source = candidate.read_text(encoding="utf-8")
            except (OSError, UnicodeError, ValueError):
                skipped.append(relative)
                continue
            if len(source) > remaining:
                skipped.append(relative)
                continue
            file_sections.append(_untrusted(relative, source))
            remaining -= len(source)

    prompt = (
        f"Dependency upgrade: {package} {old_ver} -> {new_ver}.\n"
        "The source files below are already available. Start with write_file and let "
        "the automatic test result guide the next step. Use read_file for omitted files.\n"
        f"Initial test failure:\n{_untrusted_log(failing_log[-3000:])}"
    )
    if file_sections:
        prompt += "\nPreloaded source files:\n" + "\n".join(file_sections)
    if skipped:
        prompt += (
            "\nSource files omitted from preloading because of the 30,000-character "
            "limit or because they could not be read: "
            + ", ".join(sorted(skipped))
            + ". Use read_file if needed."
        )
    return prompt
