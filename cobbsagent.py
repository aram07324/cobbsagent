"""Compatibility wrapper for the deterministic milestone-one pipeline."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import cobbs.m1_cli as _m1_cli
import cobbs.sandbox as _sandbox
from cobbs.audit import audit as _package_audit
from cobbs.audit import audit_result as _package_audit_result
from cobbs import (
    VENV_CREATE_ATTEMPTS, VENV_CREATE_TIMEOUT, _version_key, bump_requirement,
    install, installed_versions, run_tests,
)

run = _sandbox.run

def make_sandbox(repo: Path) -> tuple[Path, Path, Path]:
    """Create a fresh sandbox while preserving legacy runner injection."""
    return _sandbox.make_sandbox(repo, run_fn=run)

def audit_result(py: Path, work: Path) -> tuple[list[dict[str, Any]], bool]:
    """Audit the sandbox with the compatibility wrapper's current runner."""
    return _package_audit_result(py, work, run_fn=run)

def audit(py: Path, work: Path) -> list[dict[str, Any]]:
    """Return audit findings using the compatibility runner."""
    return _package_audit(py, work, run_fn=run)

def main() -> None:
    """Run the milestone-one command-line workflow."""
    _m1_cli.main()

def finish(report: dict[str, Any], tmp: Path) -> None:
    """Write the legacy report and clean up its temporary sandbox."""
    _m1_cli.finish(report, tmp)

if __name__ == "__main__":
    main()
