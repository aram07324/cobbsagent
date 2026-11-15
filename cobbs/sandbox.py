"""Deterministic milestone-one dependency pipeline components."""

from __future__ import annotations

from collections.abc import Callable, Sequence

import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

VENV_CREATE_TIMEOUT = 1800

VENV_CREATE_ATTEMPTS = 2

PIP_AUDIT_REQUIREMENT = "pip-audit>=2.10.1"
AUDIT_TOOLS_VENV = "audit-tools"

DOCKER_TIMEOUT = 900
DOCKER_IMAGE = "python:3.12-slim"
DOCKER_LIMITS = [
    "--memory=1g", "--cpus=1", "--pids-limit=128", "--cap-drop=ALL",
    "--security-opt=no-new-privileges",
]
_test_mode = "venv"
_docker_image = DOCKER_IMAGE
_docker_runner: Callable[..., tuple[int, str, str]] | None = None


def run(
    cmd: Sequence[str | Path], cwd: Path, timeout: int = 900,
) -> tuple[int, str, str]:
    """Run a command, return (returncode, stdout, stderr)."""
    p = subprocess.run(
        [str(c) for c in cmd], cwd=cwd, capture_output=True, text=True,
        timeout=timeout,
    )
    return p.returncode, p.stdout, p.stderr


@contextmanager
def configured_test_sandbox(
    mode: str = "venv", image: str = DOCKER_IMAGE,
    runner: Callable[..., tuple[int, str, str]] | None = None,
) -> Iterator[None]:
    """Temporarily select the test executor without leaking settings to callers."""
    global _test_mode, _docker_image, _docker_runner
    if mode not in {"venv", "docker"}:
        raise ValueError("sandbox must be 'venv' or 'docker'")
    if not image.strip():
        raise ValueError("Docker image cannot be empty")
    previous = (_test_mode, _docker_image, _docker_runner)
    _test_mode, _docker_image, _docker_runner = mode, image, runner
    try:
        yield
    finally:
        _test_mode, _docker_image, _docker_runner = previous


def _docker_command(
    work: Path, packages: Path, image: str, *, install: bool,
) -> list[str]:
    """Build a container command with no inherited host environment or network in tests."""
    command = [
        "docker", "run", "--rm", "--pull=never",
        "--network", "bridge" if install else "none",
        *DOCKER_LIMITS, "--read-only",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
        "--mount", f"type=bind,source={work.resolve()},target=/workspace,readonly",
        "--mount",
        f"type=bind,source={packages.resolve()},target=/packages"
        + ("" if install else ",readonly"),
        "--workdir", "/workspace",
    ]
    if install:
        command.extend([
            "--entrypoint", "python", image, "-m", "pip", "install",
            "--disable-pip-version-check", "--no-input", "--no-cache-dir",
            "--target", "/packages", "-r", "requirements.txt", "pytest",
        ])
    else:
        command.extend([
            "--env", "PYTHONPATH=/packages",
            "--env", "PYTHONDONTWRITEBYTECODE=1",
            "--env", "HOME=/tmp",
            "--entrypoint", "python", image, "-m", "pytest",
            "-x", "-q", "-p", "no:cacheprovider",
        ])
    return command


def run_tests_docker(
    work: Path, image: str = DOCKER_IMAGE,
    runner: Callable[..., tuple[int, str, str]] | None = None,
) -> tuple[bool, str]:
    """Install test dependencies in a bounded container, then test offline."""
    packages = work.parent / "docker-site-packages"
    marker = packages / ".ready"
    packages.mkdir(parents=True, exist_ok=True)
    run_fn = runner or run
    if not marker.is_file():
        try:
            code, out, err = run_fn(
                _docker_command(work, packages, image, install=True),
                work, timeout=DOCKER_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return False, f"Docker dependency install timed out after {DOCKER_TIMEOUT}s."
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"Docker dependency install failed: {type(exc).__name__}"
        if code:
            return False, f"Docker dependency install failed:\n{(out + err)[-3000:]}"
        marker.write_text("ready\n", encoding="utf-8")
    try:
        code, out, err = run_fn(
            _docker_command(work, packages, image, install=False),
            work, timeout=DOCKER_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False, f"Docker tests timed out after {DOCKER_TIMEOUT}s."
    except OSError as exc:
        return False, f"Docker tests could not start: {type(exc).__name__}"
    return code == 0, (out + err)[-3000:]


def make_sandbox(
    repo: Path,
    run_fn: Callable[..., tuple[int, str, str]] | None = None,
) -> tuple[Path, Path, Path]:
    """Copy a repository into a fresh venv sandbox and return its paths."""
    tmp = Path(tempfile.mkdtemp(prefix="cobbsagent_"))
    work = tmp / "repo"
    runner = run_fn or run

    def ignored(directory: str, names: list[str]) -> set[str]:
        """Exclude generated, hidden-evaluation, and repository-local venv data."""
        current = Path(directory)
        ignored_names = {
            name for name in names
            if name.casefold() in {
                ".git", ".venv", "venv", "__pycache__", ".pytest_cache",
                "eval_hidden",
            }
        }
        if current.name.casefold() == "eval":
            ignored_names.update(
                name for name in names if name.casefold() == "expected.json"
            )
        return ignored_names

    shutil.copytree(
        repo, work, ignore=ignored,
    )
    bindir = "Scripts" if sys.platform == "win32" else "bin"
    python_name = "python.exe" if sys.platform == "win32" else "python"
    py = tmp / "venv" / bindir / python_name
    last_error = ""
    for attempt in range(VENV_CREATE_ATTEMPTS):
        try:
            code, out, err = runner(
                [sys.executable, "-m", "venv", tmp / "venv"],
                tmp, timeout=VENV_CREATE_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            last_error = f"venv creation timed out after {VENV_CREATE_TIMEOUT}s"
        else:
            if code == 0 and py.is_file():
                return tmp, work, py
            last_error = (out + err)[-2000:] or f"venv creation exited with status {code}"
        if attempt + 1 < VENV_CREATE_ATTEMPTS:
            shutil.rmtree(tmp / "venv", ignore_errors=True)
    shutil.rmtree(tmp, ignore_errors=True)
    raise RuntimeError(
        f"Could not create sandbox venv after {VENV_CREATE_ATTEMPTS} attempts: {last_error}"
    )

def _audit_tool_python(work: Path) -> Path:
    """Return the Python executable for tools isolated from repo dependencies."""
    bindir = "Scripts" if sys.platform == "win32" else "bin"
    python_name = "python.exe" if sys.platform == "win32" else "python"
    return work.parent / AUDIT_TOOLS_VENV / bindir / python_name


def install(py: Path, work: Path) -> tuple[bool, str]:
    """Install repository requirements separately from audit tooling."""
    code, out, err = run(
        [
            py, "-m", "pip", "install", "-q", "-r", "requirements.txt",
            "pytest",
        ],
        work,
    )
    if code:
        return False, (out + err)[-2000:]

    audit_py = _audit_tool_python(work)
    if not audit_py.is_file():
        audit_venv = audit_py.parent.parent
        code, out, err = run(
            [sys.executable, "-m", "venv", audit_venv],
            work.parent, timeout=VENV_CREATE_TIMEOUT,
        )
        if code or not audit_py.is_file():
            return False, (out + err)[-2000:] or "Audit tools venv creation failed."

    code, out, err = run(
        [audit_py, "-m", "pip", "install", "-q", PIP_AUDIT_REQUIREMENT],
        work,
    )
    return code == 0, (out + err)[-2000:]

def run_tests(py: Path, work: Path) -> tuple[bool, str]:
    """Run pytest in the sandbox and return its status and bounded log."""
    if _test_mode == "docker":
        return run_tests_docker(work, _docker_image, _docker_runner)
    code, out, err = run([py, "-m", "pytest", "-x", "-q"], work)
    return code == 0, (out + err)[-3000:]
