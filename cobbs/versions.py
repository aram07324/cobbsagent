"""Deterministic milestone-one dependency pipeline components."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .sandbox import run


try:
    from packaging.version import Version as _Version
except ImportError:
    _Version = None

def installed_versions(py: Path, work: Path) -> dict[str, str]:
    """Return normalized installed package names and their actual versions."""
    code, out, err = run([py, "-m", "pip", "list", "--format=json"], work)
    if code != 0:
        raise RuntimeError(f"Could not list installed packages: {(out + err)[-2000:]}")
    try:
        packages = json.loads(out)
    except json.JSONDecodeError as exc:
        raise RuntimeError("pip list returned invalid JSON") from exc
    if not isinstance(packages, list):
        raise RuntimeError("pip list returned an unexpected package list")
    return {
        re.sub(r"[-_.]+", "-", item["name"]).lower(): item["version"]
        for item in packages
        if isinstance(item, dict) and isinstance(item.get("name"), str)
        and isinstance(item.get("version"), str)
    }

def _version_key(version: str) -> tuple[int, object]:
    """Return a sortable key using PEP 440 versions when packaging is installed."""
    if _Version is not None:
        try:
            return 1, _Version(version)
        except (TypeError, ValueError):
            pass
    numeric = tuple(int(part) for part in re.findall(r"\d+", version))
    return 0, numeric

def bump_requirement(work: Path, name: str, new_version: str) -> bool:
    """Rewrite `name==x` or `name>=x` style lines in requirements.txt. Sandbox copy only."""
    req = work / "requirements.txt"
    text = req.read_text()
    pattern = re.compile(
        rf"^(?P<prefix>\s*{re.escape(name)}(?:\[[^\]]+\])?\s*"
        rf"(?:(?:===|==|~=|!=|<=|>=|<|>)\s*)?)(?P<version>[^\s#;]+)",
        re.I | re.M,
    )
    if not pattern.search(text):
        return False  # transitive dependency, not pinned directly
    req.write_text(
        pattern.sub(lambda match: f"{match.group('prefix')}{new_version}", text, count=1)
    )
    return True
