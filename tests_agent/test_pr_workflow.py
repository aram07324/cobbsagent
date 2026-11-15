"""Offline tests for pull-request eligibility and guarded Git/API behavior."""

import io
import json
from pathlib import Path
from typing import Any, Sequence

import pytest

import cobbsagent_pr as pr


def _fixture(
    root: Path, *, result: str = "agent_fixed", changes: str = " M app.py\n",
    source: str = "value = 1\n",
) -> tuple[Path, dict[str, Any], Any]:
    (root / "allowed_repos.txt").write_text(
        "aram07324/cobbsagent-sandbox\n", encoding="utf-8",
    )
    case = root / "case"
    case.mkdir()
    patch = root / "code.diff"
    patch.write_text("--- a/app.py\n+++ b/app.py\n@@\n-old\n+new\n", encoding="utf-8")
    attempt = {
        "name": "PyJWT", "version": "1.7.1", "final_version": "2.15.0",
        "result": result, "proved_clean": True, "correct": True,
        "category": "breaking_medium", "vuln_ids": ["PYSEC-2026-4141"],
        "vulns_remaining": [], "verified_by": "hidden_test",
        "steps_used": 3, "backend_models": [{"backend": "ollama", "model": "fake"}],
        "rounds": [{
            "round": 1, "from": "1.7.1", "to": "2.15.0",
            "tests_passed": True, "agent_status": "fixed", "vulns_remaining": [],
        }],
        "patches": {"agent": str(patch)},
        "description": "RAW ADVISORY DESCRIPTION MUST NOT APPEAR",
    }

    def pipeline_fn(repo: Path, backend: Any) -> dict[str, Any]:
        return {
            "repo": str(repo),
            "baseline": {"tests_passed": True},
            "attempts": [attempt],
        }

    return case, {"attempt": attempt}, pipeline_fn


class FakeGit:
    def __init__(
        self, *, status: str = " M app.py\n", source: str = "value = 1\n",
        mismatched_top: bool = False, untracked: str = "",
    ) -> None:
        self.commands: list[list[str]] = []
        self.status = status
        self.source = source
        self.mismatched_top = mismatched_top
        self.untracked = untracked
        self.clone: Path | None = None

    def __call__(
        self, command: Sequence[str], timeout: int,
    ) -> tuple[int, str, str]:
        args = list(command)
        self.commands.append(args)
        clone = Path(args[2])
        subcommand = args[3:]
        if subcommand[:1] == ["clone"]:
            self.clone = clone
            (clone / ".git").mkdir()
            (clone / "app.py").write_text(self.source, encoding="utf-8")
            for relative in self.untracked.splitlines():
                target = clone / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("value = 2\n", encoding="utf-8")
            return 0, "cloned", ""
        if subcommand[:2] == ["rev-parse", "--show-toplevel"]:
            top = clone.parent if self.mismatched_top else clone
            return 0, str(top), ""
        if subcommand[:1] == ["status"]:
            return 0, self.status, ""
        if subcommand[:2] == ["ls-files", "--others"]:
            return 0, self.untracked, ""
        if subcommand[:2] == ["diff", "--no-index"]:
            return 1, "--- /dev/null\n+++ b/new_module.py\n+value = 2\n", ""
        if subcommand[:1] == ["diff"]:
            return 0, "--- a/app.py\n+++ b/app.py\n+value = 1\n", ""
        if subcommand[:1] == ["config"]:
            return 0, "Test User\n" if subcommand[-1] == "user.name" else "test@example.invalid\n", ""
        if subcommand[:1] == ["ls-remote"]:
            return 0, "", ""
        return 0, "", ""


def test_repo_url_validation_and_allow_list(tmp_path: Path) -> None:
    owner, name, canonical = pr.parse_repo_url(
        "https://github.com/aram07324/cobbsagent-sandbox.git",
    )
    assert (owner, name) == ("aram07324", "cobbsagent-sandbox")
    assert canonical.endswith("/aram07324/cobbsagent-sandbox.git")
    for url in (
        "https://github.com./aram07324/cobbsagent-sandbox",
        "https://github.com/aram07324./cobbsagent-sandbox",
        "https://github.com/aram07324/cobbsagent-sandbox/",
        "https://github.com/aram07324/cobbsagent-sandbox/extra",
        "https://github.com//aram07324/cobbsagent-sandbox",
    ):
        with pytest.raises(ValueError):
            pr.parse_repo_url(url)

    case, _, pipeline = _fixture(tmp_path)
    git = FakeGit()
    result = pr.run_pr(
        "https://github.com/ARAM07324/COBBSAGENT-SANDBOX.git", case,
        pipeline_fn=pipeline, project_root=tmp_path, git_runner=git,
    )
    assert result["outcome"] == "dry_run"
    assert any("clone" in command for command in git.commands)


def test_unlisted_repository_and_outside_case_are_rejected(tmp_path: Path) -> None:
    case, _, pipeline = _fixture(tmp_path)
    git = FakeGit()
    with pytest.raises(pr.PRWorkflowError, match="allow-list"):
        pr.run_pr(
            "https://github.com/other/demo", case, pipeline_fn=pipeline,
            project_root=tmp_path, git_runner=git,
        )
    assert not git.commands

    project = tmp_path / "project"
    project.mkdir()
    (project / "allowed_repos.txt").write_text(
        "aram07324/cobbsagent-sandbox\n", encoding="utf-8",
    )
    with pytest.raises(pr.PRWorkflowError, match="inside the project"):
        pr.run_pr(
            "https://github.com/aram07324/cobbsagent-sandbox", case,
            pipeline_fn=pipeline, project_root=project, git_runner=git,
        )
    assert not git.commands


def test_dry_run_never_pushes_or_calls_github(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    case, _, pipeline = _fixture(tmp_path)
    git = FakeGit()
    http_calls: list[Any] = []
    result = pr.run_pr(
        "https://github.com/aram07324/cobbsagent-sandbox", case,
        pipeline_fn=pipeline, project_root=tmp_path, git_runner=git,
        http_opener=lambda *args, **kwargs: http_calls.append(args),
    )
    output = capsys.readouterr().out
    assert result["outcome"] == "dry_run"
    assert "Security: bump PyJWT 1.7.1 -> 2.15.0" in output
    assert "PYSEC-2026-4141" in output
    assert "Verification: A hidden test was run." in output
    assert "The agent never merges." in output
    assert "RAW ADVISORY DESCRIPTION MUST NOT APPEAR" not in output
    assert not http_calls
    assert not any(command[3] in {"push", "commit", "add"} for command in git.commands)
    log = (tmp_path / "pr_log.jsonl").read_text(encoding="utf-8")
    assert '"outcome": "dry_run"' in log


def test_dry_run_includes_new_file_content_in_diff(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    case, _, pipeline = _fixture(tmp_path)
    git = FakeGit(status="?? new_module.py\n", untracked="new_module.py\n")
    (tmp_path / "code.diff").write_text(
        "--- /dev/null\n+++ b/new_module.py\n+value = 2\n", encoding="utf-8",
    )
    result = pr.run_pr(
        "https://github.com/aram07324/cobbsagent-sandbox", case,
        pipeline_fn=pipeline, project_root=tmp_path, git_runner=git,
    )
    assert result["outcome"] == "dry_run"
    assert "new_module.py" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("result", "correct", "flag"),
    [
        ("agent_failed", True, None),
        ("agent_fixed", False, None),
        ("overfit", True, None),
        ("agent_fixed", True, "tampering_suspected"),
    ],
)
def test_ineligible_attempts_stop_before_git(
    tmp_path: Path, result: str, correct: bool, flag: str | None,
) -> None:
    case, state, pipeline = _fixture(tmp_path, result=result)
    state["attempt"]["correct"] = correct
    if flag:
        state["attempt"][flag] = True
    git = FakeGit()
    outcome = pr.run_pr(
        "https://github.com/aram07324/cobbsagent-sandbox", case,
        pipeline_fn=pipeline, project_root=tmp_path, git_runner=git,
    )
    assert outcome["outcome"] == "ineligible"
    assert not git.commands


@pytest.mark.parametrize(
    ("status", "source", "expected"),
    [
        (" M tests/test_app.py\n", "safe\n", "protected path"),
        (" M .env\n", "safe\n", "protected path"),
        (" M app.py\n", "AIzaFAKE\n", "secret pattern"),
    ],
)
def test_forbidden_paths_and_secrets_abort(
    tmp_path: Path, status: str, source: str, expected: str,
) -> None:
    case, _, pipeline = _fixture(tmp_path)
    git = FakeGit(status=status, source=source)
    with pytest.raises(pr.PRWorkflowError, match=expected):
        pr.run_pr(
            "https://github.com/aram07324/cobbsagent-sandbox", case,
            pipeline_fn=pipeline, project_root=tmp_path, git_runner=git,
        )
    assert not any(command[3] in {"add", "commit", "push"} for command in git.commands)


def test_clone_top_level_mismatch_aborts(tmp_path: Path) -> None:
    case, _, pipeline = _fixture(tmp_path)
    git = FakeGit(mismatched_top=True)
    with pytest.raises(pr.PRWorkflowError, match="top-level mismatch"):
        pr.run_pr(
            "https://github.com/aram07324/cobbsagent-sandbox", case,
            pipeline_fn=pipeline, project_root=tmp_path, git_runner=git,
        )
    assert not any(command[3] == "switch" for command in git.commands)


def test_apply_is_confirmed_and_token_is_never_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    case, _, pipeline = _fixture(tmp_path)
    git = FakeGit()
    token = "offline-test-token"
    monkeypatch.setenv("GITHUB_TOKEN", token)
    requests: list[Any] = []

    def fake_http(request: Any, timeout: int) -> io.BytesIO:
        requests.append(request)
        return io.BytesIO(b'{"html_url":"https://github.com/aram07324/cobbsagent-sandbox/pull/12"}')

    outcome = pr.run_pr(
        "https://github.com/aram07324/cobbsagent-sandbox", case,
        apply=True, yes=True, pipeline_fn=pipeline, project_root=tmp_path,
        git_runner=git, http_opener=fake_http, stdin_is_terminal=False,
    )
    assert outcome["outcome"] == "created"
    assert len(requests) == 1
    assert requests[0].get_header("Authorization") == f"Bearer {token}"
    assert not any("--force" in command for command in git.commands)
    assert any(command[3] == "push" for command in git.commands)
    assert token not in (tmp_path / "pr_log.jsonl").read_text(encoding="utf-8")
    assert token not in capsys.readouterr().out


def test_apply_yes_does_not_skip_interactive_confirmation(tmp_path: Path) -> None:
    case, _, pipeline = _fixture(tmp_path)
    git = FakeGit()
    result = pr.run_pr(
        "https://github.com/aram07324/cobbsagent-sandbox", case,
        apply=True, yes=True, pipeline_fn=pipeline, project_root=tmp_path,
        git_runner=git, stdin_is_terminal=True, confirm=lambda _: "no",
    )
    assert result["outcome"] == "cancelled"
    assert not any(command[3] in {"add", "commit", "push"} for command in git.commands)


def test_missing_token_stops_before_local_commit_or_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case, _, pipeline = _fixture(tmp_path)
    git = FakeGit()
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(pr.PRWorkflowError, match="required for --apply"):
        pr.run_pr(
            "https://github.com/aram07324/cobbsagent-sandbox", case,
            apply=True, yes=True, pipeline_fn=pipeline, project_root=tmp_path,
            git_runner=git, stdin_is_terminal=False,
        )
    assert not any(command[3] in {"switch", "add", "commit", "push"} for command in git.commands)
