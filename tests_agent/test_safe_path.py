"""Safety checks for the milestone 2 agent's sandbox tools."""

from pathlib import Path
import subprocess

import pytest

import cobbsagent
from agent import AgentState, safe_path, write_file


def test_safe_path_rejects_parent_escape(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(ValueError, match="escapes sandbox"):
        safe_path(repo, "../x")


def test_safe_path_rejects_absolute_path(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(ValueError, match="absolute paths"):
        safe_path(repo, str((repo / "inside.py").resolve()))


@pytest.mark.parametrize("absolute", ["C:relative.py", r"\rooted.py"])
def test_safe_path_rejects_windows_drive_or_root_path(
    tmp_path: Path, absolute: str,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(ValueError, match="absolute paths"):
        safe_path(repo, absolute)


@pytest.mark.parametrize("relative", ["eval/expected.json", "eval_hidden/test.py"])
def test_safe_path_blocks_evaluation_metadata(
    tmp_path: Path, relative: str,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(ValueError, match="evaluation metadata"):
        safe_path(repo, relative)


def test_safe_path_rejects_symlink_escape(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    repo.mkdir()
    outside.mkdir()
    link = repo / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")
    with pytest.raises(ValueError, match="escapes sandbox"):
        safe_path(repo, "link/secret.py")


@pytest.mark.parametrize(
    "relative",
    ["requirements.txt", "tests/test_app.py", "test_app.py", "test-app.py"],
)
def test_write_file_refuses_protected_files(tmp_path: Path, relative: str) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    result = write_file(repo, relative, "replacement", AgentState())
    assert result.startswith("Error:")
    assert not (repo / relative).exists()


def test_make_sandbox_retries_venv_and_excludes_evaluation_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    (source / "eval").mkdir(parents=True)
    (source / "eval" / "case").mkdir()
    (source / "eval_hidden" / "case").mkdir(parents=True)
    (source / "requirements.txt").write_text("", encoding="utf-8")
    (source / "eval" / "expected.json").write_text("{}", encoding="utf-8")
    (source / "eval" / "case" / "app.py").write_text("value = 1\n", encoding="utf-8")
    (source / "eval_hidden" / "case" / "test_hidden.py").write_text(
        "def test_hidden(): pass\n", encoding="utf-8",
    )
    sandbox = tmp_path / "sandbox"
    monkeypatch.setattr(cobbsagent.tempfile, "mkdtemp", lambda **kwargs: str(sandbox))
    timeouts: list[int] = []

    def fake_run(command: list[str], cwd: Path, timeout: int):
        timeouts.append(timeout)
        if len(timeouts) == 1:
            raise subprocess.TimeoutExpired(command, timeout)
        bindir = "Scripts" if cobbsagent.sys.platform == "win32" else "bin"
        executable = "python.exe" if cobbsagent.sys.platform == "win32" else "python"
        python = sandbox / "venv" / bindir / executable
        python.parent.mkdir(parents=True)
        python.write_bytes(b"fake executable")
        return 0, "", ""

    monkeypatch.setattr(cobbsagent, "run", fake_run)
    _, work, py = cobbsagent.make_sandbox(source)

    assert timeouts == [cobbsagent.VENV_CREATE_TIMEOUT] * 2
    assert py.is_file()
    assert not (work / "eval" / "expected.json").exists()
    assert not (work / "eval_hidden").exists()
    assert (work / "eval" / "case" / "app.py").is_file()
