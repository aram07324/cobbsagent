"""Repository snapshots, human-readable diffs, and proof helpers."""

import difflib
import re
from pathlib import Path
from typing import Any

from .audit import audit_result

_IGNORED_DIRS = {"__pycache__", ".pytest_cache", ".venv", "venv", ".git"}

def _read_text_files(root: Path) -> dict[str, str]:
    """Read UTF-8 text files only, excluding generated directories and bytecode."""
    contents: dict[str, str] = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if (path.is_symlink() or not path.is_file()
                or any(part.casefold() in _IGNORED_DIRS for part in relative.parts)
                or path.suffix.casefold() in {".pyc", ".pyo"}):
            continue
        try:
            data = path.read_bytes()
            if b"\0" in data:
                continue
            text = data.decode("utf-8")
            if any(
                ord(character) < 32 and character not in "\t\n\r\f"
                for character in text
            ):
                continue
            contents[relative.as_posix()] = text
        except (OSError, UnicodeError):
            continue
    return contents

def _repository_file_paths(root: Path) -> set[str]:
    files: set[str] = set()
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if (
            path.is_file() and not path.is_symlink()
            and not any(part.casefold() in _IGNORED_DIRS for part in relative.parts)
            and path.suffix.casefold() not in {".pyc", ".pyo"}
        ):
            files.add(relative.as_posix())
    return files

def _unified_text_diff(original: Path, work: Path, requirements_only: bool) -> str:
    """Build a clean LF-only diff with labels relative to each repository."""
    before = _read_text_files(original)
    after = _read_text_files(work)
    paths = sorted(set(before) | set(after))
    lines: list[str] = []
    for relative in paths:
        is_requirements = relative.rsplit("/", 1)[-1].casefold() == "requirements.txt"
        if is_requirements != requirements_only:
            continue
        old_text = before.get(relative, "").replace("\r\n", "\n").replace("\r", "\n")
        new_text = after.get(relative, "").replace("\r\n", "\n").replace("\r", "\n")
        if old_text == new_text:
            continue
        diff_lines = list(difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"a/{relative}",
            tofile=f"b/{relative}",
            lineterm="\n",
        ))
        for index, line in enumerate(diff_lines):
            lines.append(line)
            if (index >= 2 and line[:1] in {" ", "+", "-"}
                    and not line.endswith("\n")):
                lines.extend(["\n", "\\ No newline at end of file\n"])
    return "".join(lines)

def _preserve_existing(path: Path) -> None:
    """Rename an existing output rather than discarding it."""
    if not path.exists():
        return
    backup = path.with_name(path.name + ".bak")
    suffix = 1
    while backup.exists():
        backup = path.with_name(f"{path.name}.bak.{suffix}")
        suffix += 1
    path.rename(backup)

def save_diff(original: Path, work: Path, package: str) -> dict[str, Path]:
    """Save separate relative text diffs for code edits and the requirements bump."""
    safe_repo = re.sub(r"[^A-Za-z0-9._-]+", "_", original.name)
    safe_package = re.sub(r"[^A-Za-z0-9._-]+", "_", package)
    patch_dir = Path("patches")
    texts = {
        "agent": _unified_text_diff(original, work, requirements_only=False),
        "bump": _unified_text_diff(original, work, requirements_only=True),
    }
    saved: dict[str, Path] = {}
    for kind, text in texts.items():
        output = patch_dir / f"{safe_repo}__{safe_package}__{kind}.diff"
        _preserve_existing(output)
        if not text:
            continue
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        saved[kind] = output
    return saved

def record_proof(
    attempt: dict[str, Any], py: Path, work: Path, package: str, tests_passed: bool,
) -> None:
    """Re-audit the final sandbox and record package-specific proof."""
    findings, audit_succeeded = audit_result(py, work)
    normalized_package = re.sub(r"[-_.]+", "-", package).lower()
    remaining: list[str] = []
    for finding in findings:
        normalized_name = re.sub(r"[-_.]+", "-", finding["name"]).lower()
        if normalized_name == normalized_package:
            remaining.extend(finding.get("vuln_ids", []))
    remaining = list(dict.fromkeys(remaining))
    attempt["vulns_remaining"] = remaining
    attempt["audit_succeeded"] = audit_succeeded
    attempt["proved_clean"] = bool(audit_succeeded and tests_passed and not remaining)
