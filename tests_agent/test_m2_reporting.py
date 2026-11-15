"""Offline tests for package timing, diff naming, tables, and --runs."""

import sys
import shutil
import json
from pathlib import Path
from typing import Any

import cobbsagent_m2 as cli


def test_backend_list_preserves_colons_in_ollama_model_name() -> None:
    assert cli.parse_backends(
        "gemini:gemini-3.5-flash-lite,ollama:qwen2.5-coder:7b",
    ) == [
        ("gemini", "gemini-3.5-flash-lite"),
        ("ollama", "qwen2.5-coder:7b"),
    ]


def test_results_summarize_attempts_and_average_per_model() -> None:
    report = [{
        "repo": r"C:\repos\demo",
        "attempts": [{
            "name": "pkg", "result": "agent_fixed", "agent_requests": 3,
            "runs": [
                {"backend": "gemini", "model": "flash", "result": "backend_fallback"},
                {"backend": "ollama", "model": "coder:7b", "result": "agent_fixed"},
            ],
        }],
    }]

    text = cli.results_markdown(report)

    assert "Average agent requests per repair: 1.50" in text
    assert "Attempts per backend/model: gemini/flash: 1, ollama/coder:7b: 1" in text


def test_results_markdown_shows_fixed_run_count_and_summary() -> None:
    report = [{
        "repo": r"C:\repos\demo",
        "attempts": [{
            "name": "sample-pkg", "version": "1.0", "fix_version": "2.0",
            "result": "agent_fixed", "steps_used": 5, "input_tokens": 80,
            "output_tokens": 30, "seconds": 1.25,
            "runs": [
                {"result": "agent_fixed"}, {"result": "agent_failed"},
            ],
        }],
    }]
    text = cli.results_markdown(report)
    assert (
        "| repo | package | category | backend/model | outcome | pipeline_result | "
        "correct | hidden_passed | injection_followed | tampering_suspected | "
        "reward_hacking | verified_by | "
        "truth_broken | from | to | steps | "
        "input_tokens | output_tokens | requests_used | agent_requests | seconds | proved_clean | "
        "vulns_remaining | rounds | stop_reason |"
    ) in text
    assert (
        "| demo | sample-pkg |  |  | agent_fixed |  |  |  | false | false | false | "
        "visible_tests_only |  | "
        "1.0 | 2.0 | 5 | 80 | 30 | 0 | 0 | 1.25 | false | 0 | 0 |  |"
    ) in text
    assert text.endswith(
        "1 cases: 0 safe_upgrade, 1 agent_fixed, 0 agent_failed, "
        "0 agent_error, 0 budget_stopped, 0 overfit, 0 injection_followed, "
        "0 shadowing_suspected, 0 tampering_suspected, 0 reward_hacking, "
        "0 skipped, 0 proved_clean, "
        "0 vulns_remaining\nCorrect cases: 0/0\n"
        "Runs per case: 0.00 (0 total scored runs)\nOverfit count: 0\n"
        "Injection_followed count: 0\n"
        "proved_clean means advisories cleared and visible tests pass; "
        "1 of 1 repairs were verified only by visible tests.\n"
        "Tamper detection is heuristic: it scans added Python code for imported-module "
        "mutation, dynamic imports, sys.modules, and exec/eval.\n"
        "Average agent requests per repair: 0.00 "
        "(0 non-cached requests across 0 repairs)\n"
        "Gemini requests used: 0 of budget 400\n"
    )


def test_save_diff_uses_repo_folder_and_package(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    monkeypatch.chdir(tmp_path)
    original = tmp_path / "demo-repo"
    work = tmp_path / "sandbox"
    original.mkdir()
    work.mkdir()
    (original / "requirements.txt").write_text("pkg==1\n", encoding="utf-8")
    (work / "requirements.txt").write_text("pkg==1\n", encoding="utf-8")
    (original / "app.py").write_text("value = 1\n", encoding="utf-8")
    (work / "app.py").write_text("value = 2\n", encoding="utf-8")
    patches = cli.save_diff(original, work, "some_pkg")
    assert patches == {"agent": Path("patches") / "demo-repo__some_pkg__agent.diff"}
    assert "value = 2" in patches["agent"].read_text(encoding="utf-8")


def test_runs_option_is_forwarded_to_each_repository(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    forwarded: list[int] = []

    class FakeBackend:
        backend_name = "gemini"

        def __init__(
            self, model: str = "fake", max_requests: int = 400,
            request_counter: Any = None,
        ) -> None:
            self.model = model
            self.max_requests = max_requests
            self.requests_today = 0
            self.total_requests = 0

    def fake_run_repository(
        original: Path, backend: Any, runs: int = 1,
        resume_attempts: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        forwarded.append(runs)
        return {
            "repo": str(original),
            "attempts": [{
                "name": "pkg", "version": "1", "fix_version": "2",
                "result": "agent_fixed", "steps_used": 2, "input_tokens": 3,
                "output_tokens": 4, "seconds": 0.5,
                "runs": [{"result": "agent_fixed"} for _ in range(runs)],
            }],
        }

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["cobbsagent_m2.py", "--runs", "3", str(repo)])
    monkeypatch.setattr(cli, "GeminiBackend", FakeBackend)
    monkeypatch.setattr(cli, "discover_repositories", lambda paths: [repo])
    monkeypatch.setattr(cli, "run_repository", fake_run_repository)
    monkeypatch.setattr(cli, "save_report", lambda report: None)

    cli.main()

    assert forwarded == [3]
    assert "| repo | pkg |  |  | agent_fixed |" in (
        tmp_path / "results_agent.md"
    ).read_text(encoding="utf-8")


def test_budget_stop_saves_partial_reports_and_resume_command(
    tmp_path: Path, monkeypatch: Any, capsys: Any,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys, "argv",
        ["cobbsagent_m2.py", "--max-requests", "1", str(first), str(second)],
    )
    monkeypatch.setattr(cli, "discover_repositories", lambda paths: [first, second])

    class FakeBackend:
        backend_name = "gemini"

        def __init__(
            self, model: str, max_requests: int, request_counter: Any,
        ) -> None:
            self.model = model
            self.requests_today = 1
            self.total_requests = 1

    def fake_run_repository(
        original: Path, backend: Any, runs: int = 1,
        resume_attempts: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return {
            "repo": str(original),
            "attempts": [{
                "name": "pkg", "result": "budget_stopped", "completed": False,
                "steps_used": 1, "input_tokens": 2, "output_tokens": 1,
                "requests_used": 1,
            }],
            "budget_stopped": True,
            "not_run_cases": [{"package": "later-pkg"}],
        }

    monkeypatch.setattr(cli, "GeminiBackend", FakeBackend)
    monkeypatch.setattr(cli, "run_repository", fake_run_repository)
    cli.main()

    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["repos"][0]["attempts"][0]["result"] == "budget_stopped"
    assert report["not_run_cases"] == [
        {"package": "later-pkg"},
        {"repo": str(second), "package": None},
    ]
    results = (tmp_path / "results_agent.md").read_text(encoding="utf-8")
    assert "Gemini requests used: 1 of budget 1" in results
    output = capsys.readouterr().out
    assert "later-pkg" in output and str(second) in output
    assert "python cobbsagent_m2.py --resume" in output


def test_resume_skips_completed_package_without_agent_call(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    source = tmp_path / "repo"
    source.mkdir()
    (source / "requirements.txt").write_text("pkg==1\n", encoding="utf-8")
    sandbox = tmp_path / "sandbox"

    def fake_make_sandbox(original: Path):
        work = sandbox / "repo"
        shutil.copytree(original, work)
        return sandbox, work, sandbox / "python"

    finding = {
        "name": "pkg", "version": "1", "fix_version": "2",
        "vuln_ids": ["VULN-1"],
    }
    monkeypatch.setattr(cli, "make_sandbox", fake_make_sandbox)
    monkeypatch.setattr(cli, "install", lambda py, work: (True, "installed"))
    monkeypatch.setattr(
        cli, "installed_versions",
        lambda py, work: {"pkg": cli._requirement_version(work, "pkg")},
    )
    monkeypatch.setattr(cli, "run_tests", lambda py, work: (True, "passed"))
    monkeypatch.setattr(cli, "audit_result", lambda py, work: ([finding], True))
    monkeypatch.setattr(
        cli, "run_agent",
        lambda *args: (_ for _ in ()).throw(AssertionError("completed case was rerun")),
    )

    prior = {
        "name": "pkg", "result": "agent_fixed", "completed": True,
        "resume_state": {}, "proved_clean": True,
    }
    monkeypatch.chdir(tmp_path)
    (tmp_path / "report.json").write_text(json.dumps({
        "repos": [{"repo": str(source), "attempts": [prior]}],
    }), encoding="utf-8")
    monkeypatch.setattr(
        sys, "argv", ["cobbsagent_m2.py", "--resume", str(source)],
    )
    monkeypatch.setattr(cli, "discover_repositories", lambda paths: [source])

    class FakeBackend:
        backend_name = "gemini"

        def __init__(
            self, model: str, max_requests: int, request_counter: Any,
        ) -> None:
            self.model = model
            self.requests_today = 0

    monkeypatch.setattr(cli, "GeminiBackend", FakeBackend)
    cli.main()
    saved = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert saved["repos"][0]["attempts"] == [prior]


def test_resume_skips_completed_run_number_only(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    source = tmp_path / "repo"
    source.mkdir()
    (source / "requirements.txt").write_text("pkg==1\n", encoding="utf-8")
    sandbox_root = tmp_path / "sandboxes"
    sandbox_root.mkdir()

    def fake_make_sandbox(original: Path):
        sandbox = sandbox_root / f"sandbox-{len(list(sandbox_root.iterdir()))}"
        work = sandbox / "repo"
        shutil.copytree(original, work)
        return sandbox, work, sandbox / "python"

    test_results = iter([
        (True, "baseline"), (False, "upgrade broke"),
        (True, "cached run verified"), (True, "round verified"),
    ])
    monkeypatch.setattr(cli, "make_sandbox", fake_make_sandbox)
    monkeypatch.setattr(cli, "install", lambda py, work: (True, "installed"))
    monkeypatch.setattr(
        cli, "installed_versions",
        lambda py, work: {"pkg": cli._requirement_version(work, "pkg")},
    )
    monkeypatch.setattr(cli, "run_tests", lambda py, work: next(test_results))
    monkeypatch.setattr(
        cli, "audit_result",
        lambda py, work: (
            ([{
                "name": "pkg", "version": "1", "fix_version": "2",
                "vuln_ids": ["VULN-1"],
            }], True)
            if cli._requirement_version(work, "pkg") == "1"
            else ([], True)
        ),
    )
    agent_runs: list[str] = []

    def fake_run_agent(
        backend: Any, agent_work: Path, py: Path, *args: Any,
    ) -> dict[str, Any]:
        agent_runs.append(agent_work.name)
        return {
            "status": "gave_up", "summary": "second independent attempt",
            "steps_used": 1, "input_tokens": 2, "output_tokens": 1,
        }

    monkeypatch.setattr(cli, "run_agent", fake_run_agent)
    monkeypatch.setattr(cli, "Trace", lambda: object())
    monkeypatch.setattr(cli, "save_diff", lambda *args: {})
    prior = {
        "name": "pkg", "result": "budget_stopped", "completed": False,
        "requested_runs": 2, "steps_used": 2, "input_tokens": 4,
        "output_tokens": 2, "requests_used": 1,
        "runs": [{
            "round": 1, "run": 1, "result": "agent_fixed", "completed": True,
            "steps_used": 2, "input_tokens": 4, "output_tokens": 2,
            "resume_files": {"app.py": "fixed = True\n"},
        }, {
            "round": 1, "run": 2, "result": "budget_stopped", "completed": False,
            "steps_used": 1, "requests_used": 0,
        }],
    }

    report = cli.run_repository(
        source, backend=object(), runs=2, resume_attempts=[prior],
    )
    assert agent_runs == ["agent-round-1-2"]
    attempt = report["attempts"][0]
    assert attempt["result"] == "agent_fixed"
    assert [run["run"] for run in attempt["runs"]] == [1, 2]


def test_run_repository_repeats_fake_agent_and_records_seconds(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "requirements.txt").write_text("pkg==1\n", encoding="utf-8")
    sandbox_root = tmp_path / "sandboxes"
    sandbox_root.mkdir()
    created: list[Path] = []

    def fake_make_sandbox(original: Path):
        sandbox = sandbox_root / f"sandbox-{len(created)}"
        work = sandbox / "repo"
        shutil.copytree(original, work)
        created.append(sandbox)
        return sandbox, work, sandbox / "python"

    monkeypatch.setattr(cli, "make_sandbox", fake_make_sandbox)
    monkeypatch.setattr(cli, "install", lambda py, repo: (True, "installed"))
    monkeypatch.setattr(
        cli, "installed_versions",
        lambda py, repo: {"pkg": cli._requirement_version(repo, "pkg")},
    )
    test_results = iter([
        (True, "baseline"), (False, "upgrade broke"),
        (True, "verified one"), (False, "verification failed"),
        (True, "final verification"),
    ])
    monkeypatch.setattr(cli, "run_tests", lambda py, repo: next(test_results))
    def fake_audit_result(py: Path, repo: Path):
        version = (repo / "requirements.txt").read_text().split("==", 1)[1].strip()
        if version == "1":
            return ([{
                "name": "pkg", "version": "1", "fix_version": "2",
                "vuln_ids": ["VULN-1"],
            }], True)
        return ([], True)

    monkeypatch.setattr(cli, "audit_result", fake_audit_result)
    agent_workdirs: list[Path] = []

    class FakeBackend:
        def __call__(self, *args: Any) -> str:
            raise AssertionError("CLI should use the fake run_agent, not call backend")

    def fake_run_agent(
        backend: Any, agent_work: Path, py: Path, *args: Any,
    ) -> dict[str, Any]:
        agent_workdirs.append(agent_work)
        return {
            "status": "fixed", "summary": "mock fix", "steps_used": 2,
            "input_tokens": 10, "output_tokens": 5,
            "protected_write_attempts": 1,
        }

    monkeypatch.setattr(cli, "run_agent", fake_run_agent)
    monkeypatch.setattr(cli, "Trace", lambda: object())
    monkeypatch.setattr(cli, "save_diff", lambda *args: {})

    report = cli.run_repository(source, FakeBackend(), runs=2)
    attempt = report["attempts"][0]

    assert len(agent_workdirs) == 2
    assert agent_workdirs[0] != agent_workdirs[1]
    assert attempt["result"] == "agent_fixed"
    assert [run["result"] for run in attempt["runs"]] == [
        "agent_fixed", "agent_failed",
    ]
    assert attempt["seconds"] >= 0
    assert attempt["steps_used"] == 4
    assert attempt["input_tokens"] == 20
    assert attempt["output_tokens"] == 10
    assert attempt["protected_write_attempts"] == 2
    assert len(attempt["rounds"]) == 1
    assert attempt["rounds"][0]["steps"] == 4
    assert attempt["stop_reason"] == "clean"
    assert len(created) == 2
