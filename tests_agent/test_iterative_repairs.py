"""Offline tests for fresh, iterative dependency repair rounds."""

import re
import shutil
from pathlib import Path
from typing import Any

import cobbsagent_m2 as cli
from agent import (
    AgentRunError, BackendChain, RateLimitExceeded, build_task_prompt,
)


def _setup(
    tmp_path: Path, monkeypatch: Any, requirements: str,
) -> tuple[Path, list[tuple[str, Path]]]:
    source = tmp_path / "source"
    source.mkdir()
    (source / "requirements.txt").write_text(requirements, encoding="utf-8")
    sandbox_root = tmp_path / "sandboxes"
    sandbox_root.mkdir()
    created: list[tuple[str, Path]] = []

    def make_sandbox(repo: Path):
        sandbox = sandbox_root / f"sandbox-{len(created)}"
        work = sandbox / "repo"
        shutil.copytree(repo, work)
        py = sandbox / "python"
        created.append(((work / "requirements.txt").read_text(), py))
        return sandbox, work, py

    monkeypatch.setattr(cli, "make_sandbox", make_sandbox)
    monkeypatch.setattr(cli, "install", lambda py, work: (True, "installed"))
    monkeypatch.setattr(
        cli, "installed_versions",
        lambda py, work: {
            cli._package_key(match.group(1)): match.group(2)
            for line in (work / "requirements.txt").read_text().splitlines()
            if (match := re.match(
                r"\s*([A-Za-z0-9][A-Za-z0-9_.-]*)==([^\s#]+)", line,
            ))
        },
    )
    monkeypatch.setattr(cli, "save_diff", lambda *args: {})
    monkeypatch.setattr(cli, "Trace", lambda: object())
    return source, created


def _version(work: Path, package: str) -> str:
    for line in (work / "requirements.txt").read_text().splitlines():
        if line.casefold().startswith(package.casefold() + "=="):
            return line.split("==", 1)[1]
    raise AssertionError(f"{package} pin not found")


def _finding(package: str, version: str, fix: str | None, vuln: str) -> dict:
    return {
        "name": package, "version": version, "fix_version": fix,
        "vuln_ids": [vuln],
    }


def test_second_round_resolves_new_advisory(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    source, sandboxes = _setup(tmp_path, monkeypatch, "pkg==1\n")

    def audit(py: Path, work: Path):
        version = _version(work, "pkg")
        if version == "1":
            findings = [_finding("pkg", "1", "2", "OLD")]
        elif version == "2":
            findings = [_finding("pkg", "2", "3", "NEW")]
        else:
            findings = []
        return findings, True

    monkeypatch.setattr(cli, "audit_result", audit)
    monkeypatch.setattr(cli, "run_tests", lambda py, work: (True, "passed"))
    monkeypatch.setattr(
        cli, "run_agent",
        lambda *args: (_ for _ in ()).throw(AssertionError("tests did not break")),
    )

    report = cli.run_repository(source, backend=object())
    attempt = report["attempts"][0]

    assert [round_info["to"] for round_info in attempt["rounds"]] == ["2", "3"]
    assert [round_info["from"] for round_info in attempt["rounds"]] == ["1", "2"]
    assert attempt["stop_reason"] == "clean"
    assert attempt["final_version"] == "3"
    assert attempt["vulns_remaining"] == []
    assert attempt["proved_clean"] is True
    assert len(sandboxes) == 3
    assert len({str(py) for _, py in sandboxes}) == 3
    assert all(not py.parent.exists() for _, py in sandboxes)


def test_vulnerability_without_a_fix_stops_immediately(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    source, _ = _setup(tmp_path, monkeypatch, "pkg==1\n")
    monkeypatch.setattr(
        cli, "audit_result",
        lambda py, work: ([_finding("pkg", "1", None, "NO-FIX")], True),
    )
    monkeypatch.setattr(cli, "run_tests", lambda py, work: (True, "passed"))

    attempt = cli.run_repository(source, backend=object())["attempts"][0]

    assert attempt["rounds"] == []
    assert attempt["stop_reason"] == "no_fix_available"
    assert attempt["vulns_remaining"] == ["NO-FIX"]
    assert attempt["proved_clean"] is False


def test_round_limit_stops_after_three_upgrades(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    source, _ = _setup(tmp_path, monkeypatch, "pkg==1\n")

    def audit(py: Path, work: Path):
        version = _version(work, "pkg")
        next_version = str(int(version) + 1)
        return ([_finding("pkg", version, next_version, f"V{version}")], True)

    monkeypatch.setattr(cli, "audit_result", audit)
    monkeypatch.setattr(cli, "run_tests", lambda py, work: (True, "passed"))

    attempt = cli.run_repository(source, backend=object())["attempts"][0]

    assert len(attempt["rounds"]) == cli.MAX_ROUNDS == 3
    assert attempt["final_version"] == "4"
    assert attempt["stop_reason"] == "max_rounds"
    assert attempt["proved_clean"] is False


def test_agent_failure_in_second_round_stops_unclean(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    source, _ = _setup(tmp_path, monkeypatch, "pkg==1\n")

    def audit(py: Path, work: Path):
        version = _version(work, "pkg")
        return ([_finding(
            "pkg", version, str(int(version) + 1), f"V{version}",
        )], True)

    tests = iter([
        (True, "baseline"),
        (False, "round one break"), (True, "round one independent check"),
        (True, "round one final verification"),
        (False, "round two break"), (False, "round two independent check fails"),
    ])
    monkeypatch.setattr(cli, "audit_result", audit)
    monkeypatch.setattr(cli, "run_tests", lambda py, work: next(tests))
    outcomes = iter([
        {
            "status": "fixed", "summary": "round one repaired",
            "steps_used": 2, "input_tokens": 7, "output_tokens": 3,
        },
        {
            "status": "gave_up", "summary": "round two could not be repaired",
            "steps_used": 3, "input_tokens": 11, "output_tokens": 7,
        },
    ])
    monkeypatch.setattr(cli, "run_agent", lambda *args: next(outcomes))

    attempt = cli.run_repository(source, backend=object())["attempts"][0]

    assert len(attempt["rounds"]) == 2
    assert attempt["rounds"][0]["tests_passed"] is True
    assert attempt["rounds"][0]["agent_status"] == "fixed"
    assert attempt["rounds"][0]["steps"] == 2
    assert attempt["rounds"][1]["agent_status"] == "gave_up"
    assert attempt["rounds"][1]["tests_passed"] is False
    assert attempt["rounds"][1]["steps"] == 3
    assert attempt["stop_reason"] == "agent_failed"
    assert attempt["proved_clean"] is False
    assert attempt["steps_used"] == 5
    assert attempt["input_tokens"] == 18
    assert attempt["output_tokens"] == 10


def test_fresh_parent_install_resolves_stale_transitive_finding(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    source, sandboxes = _setup(tmp_path, monkeypatch, "requests==1\n")

    def audit(py: Path, work: Path):
        if _version(work, "requests") == "1":
            return ([
                _finding("requests", "1", "2", "REQ-VULN"),
                _finding("urllib3", "1.26.20", "2.0", "URLLIB3-VULN"),
            ], True)
        return ([], True)

    audited_envs: list[Path] = []

    def tracked_audit(py: Path, work: Path):
        audited_envs.append(py)
        return audit(py, work)

    monkeypatch.setattr(cli, "audit_result", tracked_audit)
    monkeypatch.setattr(cli, "run_tests", lambda py, work: (True, "passed"))
    monkeypatch.setattr(
        cli, "installed_versions",
        lambda py, work: (
            {"requests": "1", "urllib3": "1.26.20"}
            if _version(work, "requests") == "1"
            else {"requests": "2", "urllib3": "2.0"}
        ),
    )

    attempts = cli.run_repository(source, backend=object())["attempts"]
    transitive = next(item for item in attempts if item["name"] == "urllib3")

    assert len(sandboxes) == 2
    assert len(audited_envs) == 2
    assert audited_envs[0] != audited_envs[1]
    assert transitive["result"] == "resolved_by_parent_bump"
    assert transitive["resolved_by_parent"] == "requests"
    assert transitive["final_version"] == "2.0"
    assert transitive["vulns_remaining"] == []
    assert transitive["proved_clean"] is True
    transitive_row = next(
        line for line in cli.results_markdown([{
            "repo": str(source), "attempts": [transitive],
        }]).splitlines()
        if "| urllib3 |" in line
    )
    assert "| 1.26.20 | 2.0 |" in transitive_row


def test_transitive_labels_require_a_changed_version_and_clear_audit(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    source, _ = _setup(tmp_path, monkeypatch, "requests==1\n")

    def audit(py: Path, work: Path):
        if _version(work, "requests") == "1":
            return ([
                _finding("requests", "1", "2", "REQ-VULN"),
                _finding("urllib3", "1.26.20", "2.0", "URLLIB3-VULN"),
            ], True)
        return ([], True)

    monkeypatch.setattr(cli, "audit_result", audit)
    monkeypatch.setattr(cli, "run_tests", lambda py, work: (True, "passed"))
    monkeypatch.setattr(
        cli, "installed_versions",
        lambda py, work: {
            "requests": _version(work, "requests"), "urllib3": "1.26.20",
        },
    )

    attempts = cli.run_repository(source, backend=object())["attempts"]
    transitive = next(item for item in attempts if item["name"] == "urllib3")

    assert transitive["result"] == "unchanged_transitive"
    assert transitive["final_version"] == "1.26.20"
    assert "resolved_by_parent" not in transitive


def test_transitive_remaining_vulnerability_uses_installed_version(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    source, _ = _setup(tmp_path, monkeypatch, "requests==1\n")

    def audit(py: Path, work: Path):
        if _version(work, "requests") == "1":
            return ([
                _finding("requests", "1", "2", "REQ-VULN"),
                _finding("urllib3", "1.26.20", "2.0", "URLLIB3-VULN"),
            ], True)
        return ([
            _finding("urllib3", "1.26.20", "2.0", "URLLIB3-VULN"),
        ], True)

    monkeypatch.setattr(cli, "audit_result", audit)
    monkeypatch.setattr(cli, "run_tests", lambda py, work: (True, "passed"))
    monkeypatch.setattr(
        cli, "installed_versions",
        lambda py, work: {
            "requests": _version(work, "requests"), "urllib3": "1.26.20",
        },
    )

    attempts = cli.run_repository(source, backend=object())["attempts"]
    transitive = next(item for item in attempts if item["name"] == "urllib3")
    row = next(
        line for line in cli.results_markdown([{
            "repo": str(source), "attempts": [transitive],
        }]).splitlines()
        if "| urllib3 |" in line
    )

    assert transitive["result"] == "still_vulnerable"
    assert transitive["final_version"] == "1.26.20"
    assert "| 1.26.20 | 1.26.20 |" in row


def test_pip_audit_description_never_reaches_agent_prompt(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    source, _ = _setup(tmp_path, monkeypatch, "pkg==1\n")
    sentinel = "PRIVATE-ADVISORY-DESCRIPTION-DO-NOT-PROMPT"
    audit_calls = 0

    def audit(py: Path, work: Path):
        nonlocal audit_calls
        audit_calls += 1
        return ([{
            **_finding("pkg", _version(work, "pkg"), "2", "VULN"),
            "description": sentinel,
        }], True)

    prompts: list[str] = []

    def fake_agent(
        backend: Any, work: Path, py: Path, package: str, old: str,
        new: str, failing_log: str, trace: Any,
    ) -> dict[str, Any]:
        prompts.append(build_task_prompt(package, old, new, failing_log))
        return {
            "status": "gave_up", "summary": "failed",
            "steps_used": 1, "input_tokens": 2, "output_tokens": 1,
        }

    tests = iter([
        (True, "baseline"), (False, "actual test failure"),
        (False, "independent test failure"),
    ])
    monkeypatch.setattr(cli, "audit_result", audit)
    monkeypatch.setattr(cli, "run_tests", lambda py, work: next(tests))
    monkeypatch.setattr(cli, "run_agent", fake_agent)

    cli.run_repository(source, backend=object())

    assert audit_calls == 2
    assert prompts and sentinel not in prompts[0]


def test_rate_limit_fallback_occurs_between_backend_attempts(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    source, _ = _setup(tmp_path, monkeypatch, "pkg==1\n")
    (source / "app.py").write_text("value = 1\n", encoding="utf-8")

    def audit(py: Path, work: Path):
        if _version(work, "pkg") == "1":
            return ([_finding("pkg", "1", "2", "VULN")], True)
        return ([], True)

    tests = iter([
        (True, "baseline"), (False, "upgrade break"),
        (True, "independent verification"), (True, "final verification"),
    ])
    monkeypatch.setattr(cli, "audit_result", audit)
    monkeypatch.setattr(cli, "run_tests", lambda py, work: next(tests))

    def gemini_call(system: str, messages: list[dict[str, str]]) -> str:
        raise RateLimitExceeded("HTTP 429 after retries")

    def ollama_call(system: str, messages: list[dict[str, str]]) -> str:
        return "response"

    gemini = type("FakeGemini", (), {
        "backend_name": "gemini", "model": "gemini-test",
        "total_requests": 1, "requests_today": 1,
        "__call__": lambda self, system, messages: gemini_call(system, messages),
    })()
    ollama = type("FakeOllama", (), {
        "backend_name": "ollama", "model": "ollama-test",
        "total_requests": 1, "requests_today": 0,
        "__call__": lambda self, system, messages: ollama_call(system, messages),
    })()
    chain = BackendChain([gemini, ollama])
    backend_order: list[str] = []

    def fake_agent(
        backend: Any, agent_work: Path, py: Path, *args: Any,
    ) -> dict[str, Any]:
        backend_order.append(backend.backend_name)
        if backend.backend_name == "gemini":
            try:
                backend("system", [{"role": "user", "text": "prompt"}])
            except RateLimitExceeded as exc:
                raise AgentRunError("rate limited", 1, 0, 0) from exc
        backend("system", [{"role": "user", "text": "prompt"}])
        return {
            "status": "fixed", "summary": "repaired",
            "steps_used": 1, "input_tokens": 2, "output_tokens": 1,
        }

    monkeypatch.setattr(cli, "run_agent", fake_agent)
    report = cli.run_repository(source, chain)
    attempt = report["attempts"][0]

    assert backend_order == ["gemini", "ollama"]
    assert [run["result"] for run in attempt["runs"]] == [
        "backend_fallback", "agent_fixed",
    ]
    assert [(run["backend"], run["model"]) for run in attempt["runs"]] == [
        ("gemini", "gemini-test"), ("ollama", "ollama-test"),
    ]
    assert [item["backend"] for item in attempt["backend_models"]] == [
        "gemini", "ollama",
    ]
