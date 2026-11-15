"""Deterministic milestone-one dependency pipeline components."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any

from .sandbox import install, make_sandbox, run_tests

from .audit import audit_result

from .versions import bump_requirement

def main() -> None:
    """Run the deterministic milestone-one repository workflow."""
    ap = argparse.ArgumentParser()
    ap.add_argument("repo", type=Path)
    args = ap.parse_args()

    tmp, work, py = make_sandbox(args.repo.resolve())
    report = {"repo": str(args.repo), "baseline": None, "attempts": []}

    ok, log = install(py, work)
    if not ok:
        report["baseline"] = {"installed": False, "log": log}
        print("Baseline install failed.\n", log)
        return finish(report, tmp)

    passed, log = run_tests(py, work)
    report["baseline"] = {"installed": True, "tests_passed": passed, "log": log}
    if not passed:
        print("Baseline tests already failing. Fix those first.")
        return finish(report, tmp)

    findings, audit_succeeded = audit_result(py, work)
    report["baseline"]["audit_succeeded"] = audit_succeeded
    if not audit_succeeded:
        print("Dependency audit failed; refusing to treat the repository as clean.")
        return finish(report, tmp)
    print(f"Found {len(findings)} vulnerable package(s).")

    for f in findings:
        attempt = dict(f)
        if not f["fix_version"]:
            attempt["result"] = "no_fix_available"
        elif not bump_requirement(work, f["name"], f["fix_version"]):
            attempt["result"] = "transitive_dependency_skipped"
        else:
            ok, ilog = install(py, work)
            if not ok:
                attempt.update(result="install_failed", log=ilog)
            else:
                passed, tlog = run_tests(py, work)
                attempt.update(result="safe_upgrade" if passed else "tests_broke", log=tlog)
        print(f"  {f['name']} {f['version']} -> {f['fix_version']}: {attempt['result']}")
        report["attempts"].append(attempt)

    finish(report, tmp)

def finish(report: dict[str, Any], tmp: Path) -> None:
    """Write the report and remove the temporary sandbox."""
    out = Path("report.json")
    out.write_text(json.dumps(report, indent=2))
    print(f"Report written to {out.resolve()}")
    shutil.rmtree(tmp, ignore_errors=True)
