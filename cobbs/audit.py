"""Deterministic milestone-one dependency pipeline components."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import json
from pathlib import Path

from .versions import _version_key
from .sandbox import _audit_tool_python, run


def audit_result(
    py: Path, work: Path,
    run_fn: Callable[..., tuple[int, str, str]] | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Audit only the dependency closure resolved from the target requirements."""
    # pip-audit exits non-zero when it finds vulns, so parse stdout regardless.
    runner = run_fn or run
    audit_py = _audit_tool_python(work)
    if not audit_py.is_file():
        audit_py = py
    command = [
        audit_py, "-m", "pip_audit", "--requirement", work / "requirements.txt",
        "--format", "json", "--desc", "off", "--progress-spinner", "off",
    ]
    code, out, _ = runner(command, work)
    if code not in {0, 1}:
        return [], False
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return [], False
    if not isinstance(data, dict) or not isinstance(data.get("dependencies"), list):
        return [], False

    findings = []
    for dep in data.get("dependencies", []):
        vulns = dep.get("vulns", [])
        if not vulns:
            continue
        per_vulnerability_fixes = [
            min(fixes, key=_version_key)
            for vuln in vulns
            if (fixes := vuln.get("fix_versions", []))
        ]
        finding = {
            "name": dep["name"],
            "version": dep["version"],
            "fix_version": (
                max(per_vulnerability_fixes, key=_version_key)
                if per_vulnerability_fixes else None
            ),
            "vuln_ids": [v["id"] for v in vulns],
        }
        unfixable = [
            vuln["id"] for vuln in vulns if not vuln.get("fix_versions")
        ]
        if unfixable:
            finding["unfixable_vuln_ids"] = unfixable
        findings.append(finding)
    return findings, True

def audit(
    py: Path, work: Path,
    run_fn: Callable[..., tuple[int, str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Return vulnerable dependencies with the most comprehensive available fix."""
    findings, succeeded = audit_result(py, work, run_fn=run_fn)
    if not succeeded:
        raise RuntimeError("pip-audit failed or returned invalid JSON")
    return findings
