"""Tests for clean source diffs and requirements-only patch generation."""

from pathlib import Path

import cobbsagent as m1
import cobbsagent_m2 as cli


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


def test_saves_clean_agent_and_requirement_diffs(
    tmp_path: Path, monkeypatch,
) -> None:
    original = tmp_path / "jwt-case"
    work = tmp_path / "sandbox-repo"
    original.mkdir()
    work.mkdir()
    _write(original / "requirements.txt", "PyJWT==1.7.1  # pinned\n")
    _write(work / "requirements.txt", "PyJWT==1.7.1  # pinned\n")
    _write(original / "app.py", "value = 1\n")
    _write(work / "app.py", "value = 2\n")
    _write(original / ".pytest_cache" / "state", "old cache\n")
    _write(work / ".pytest_cache" / "state", "new cache\n")
    _write(original / "__pycache__" / "module.pyc", "old bytecode\n")
    _write(work / "__pycache__" / "module.pyc", "new bytecode\n")
    _write(original / "venv" / "state.txt", "old venv\n")
    _write(work / "venv" / "state.txt", "new venv\n")
    _write(original / ".git" / "state", "old git metadata\n")
    _write(work / ".git" / "state", "new git metadata\n")
    (original / "image.bin").write_bytes(b"\x00old binary")
    (work / "image.bin").write_bytes(b"\x00new binary")

    assert m1.bump_requirement(work, "pyjwt", "2.4.0")
    assert (work / "requirements.txt").read_text(encoding="utf-8") == (
        "PyJWT==2.4.0  # pinned\n"
    )
    monkeypatch.chdir(tmp_path)
    saved = cli.save_diff(original, work, "PyJWT")

    assert saved == {
        "agent": Path("patches") / "jwt-case__PyJWT__agent.diff",
        "bump": Path("patches") / "jwt-case__PyJWT__bump.diff",
    }
    agent_diff = saved["agent"].read_text(encoding="utf-8")
    bump_diff = saved["bump"].read_text(encoding="utf-8")
    assert "--- a/app.py\n+++ b/app.py\n" in agent_diff
    assert "value = 1" in agent_diff and "value = 2" in agent_diff
    assert "requirements.txt" not in agent_diff
    assert ".pytest_cache" not in agent_diff
    assert "__pycache__" not in agent_diff
    assert "venv/" not in agent_diff
    assert ".git/" not in agent_diff
    assert "image.bin" not in agent_diff
    assert str(tmp_path) not in agent_diff + bump_diff
    assert "cobbsagent_" not in agent_diff + bump_diff
    assert "\r" not in agent_diff + bump_diff
    assert bump_diff.startswith("--- a/requirements.txt\n+++ b/requirements.txt\n")
    assert "-PyJWT==1.7.1  # pinned\n+PyJWT==2.4.0  # pinned\n" in bump_diff
    assert "app.py" not in bump_diff


def test_unchanged_repositories_write_no_patch_files(
    tmp_path: Path, monkeypatch,
) -> None:
    original = tmp_path / "unchanged"
    work = tmp_path / "sandbox"
    original.mkdir()
    work.mkdir()
    _write(original / "app.py", "value = 1\n")
    _write(work / "app.py", "value = 1\n")
    _write(original / "requirements.txt", "pkg==1\n")
    _write(work / "requirements.txt", "pkg==1\n")
    monkeypatch.chdir(tmp_path)

    assert cli.save_diff(original, work, "pkg") == {}
    assert not (tmp_path / "patches").exists()


def test_diff_marks_files_without_trailing_newlines(
    tmp_path: Path, monkeypatch,
) -> None:
    original = tmp_path / "repo"
    work = tmp_path / "sandbox"
    original.mkdir()
    work.mkdir()
    (original / "app.py").write_text("old", encoding="utf-8")
    (work / "app.py").write_text("new", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    patches = cli.save_diff(original, work, "pkg")
    diff = patches["agent"].read_text(encoding="utf-8")
    assert "-old\n\\ No newline at end of file\n" in diff
    assert "+new\n\\ No newline at end of file\n" in diff
