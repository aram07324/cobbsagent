"""The Docker executor is verified with a fake runner and never starts Docker."""

import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

from cobbs.sandbox import configured_test_sandbox, run_tests


class FakeDocker:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], Path, int]] = []

    def __call__(
        self, command: Sequence[str], cwd: Path, timeout: int,
    ) -> tuple[int, str, str]:
        self.calls.append((list(command), cwd, timeout))
        return 0, "ok", ""


def test_docker_installs_once_and_runs_tests_without_network_or_host_env(
    tmp_path: Path,
) -> None:
    work = tmp_path / "repo"
    work.mkdir()
    (work / "requirements.txt").write_text("example==1\n", encoding="utf-8")
    fake = FakeDocker()

    with configured_test_sandbox("docker", "python:test", fake):
        assert run_tests(Path("unused"), work) == (True, "ok")
        assert run_tests(Path("unused"), work) == (True, "ok")

    assert len(fake.calls) == 3
    install, test, test_again = [call[0] for call in fake.calls]
    assert install[0:3] == ["docker", "run", "--rm"]
    assert install[install.index("--network") + 1] == "bridge"
    assert "--memory=1g" in install and "--pids-limit=128" in install
    assert "--env-file" not in install and "--env" not in install
    assert test[test.index("--network") + 1] == "none"
    assert "--read-only" in test
    assert test[test.index("--env") + 1] == "PYTHONPATH=/packages"
    assert "python:test" in test and test[-4:] == ["-x", "-q", "-p", "no:cacheprovider"]
    assert "python:test" in test_again
    assert fake.calls[0][2] == 900


def test_docker_install_timeout_is_reported(tmp_path: Path) -> None:
    work = tmp_path / "repo"
    work.mkdir()

    def timeout_runner(
        command: Sequence[str], cwd: Path, timeout: int,
    ) -> tuple[int, str, str]:
        raise subprocess.TimeoutExpired(command, timeout)

    with configured_test_sandbox("docker", "python:test", timeout_runner):
        passed, log = run_tests(Path("unused"), work)
    assert not passed
    assert "timed out" in log


def test_vanilla_venv_is_restored_after_docker_context() -> None:
    import tempfile

    with configured_test_sandbox("docker", "python:test", FakeDocker()):
        pass
    with tempfile.TemporaryDirectory() as directory:
        work = Path(directory)
        (work / "test_smoke.py").write_text(
            "def test_smoke():\n    assert True\n", encoding="utf-8",
        )
        passed, _ = run_tests(Path(sys.executable), work)
    assert passed
    try:
        with configured_test_sandbox("invalid"):
            pass
    except ValueError as exc:
        assert "venv" in str(exc) and "docker" in str(exc)
    else:
        raise AssertionError("invalid mode should be rejected")
