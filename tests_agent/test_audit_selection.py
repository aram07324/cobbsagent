"""Tests for fix-version selection and post-upgrade audit proof."""

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
import cobbsagent
import cobbsagent_m2
import cobbs.sandbox
import eval_harness


def _pip_audit_json(py: Path, work: Path) -> str:
    return json.dumps({
        "dependencies": [{
            "name": "PyJWT",
            "version": "1.7.1",
            "vulns": [
                {"id": "VULN-A", "fix_versions": ["2.0", "1.9"]},
                {"id": "VULN-B", "fix_versions": ["2.2", "2.1"]},
                {"id": "VULN-C", "fix_versions": []},
            ],
        }],
    })


def test_audit_uses_maximum_of_each_vulnerability_minimum(
    monkeypatch: Any, tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        cobbsagent, "run", lambda *args: (1, _pip_audit_json(*args[:2]), ""),
    )
    findings, succeeded = cobbsagent.audit_result(tmp_path / "python", tmp_path)

    assert succeeded
    assert findings[0]["fix_version"] == "2.1"
    assert findings[0]["vuln_ids"] == ["VULN-A", "VULN-B", "VULN-C"]
    assert findings[0]["unfixable_vuln_ids"] == ["VULN-C"]


def test_audit_keeps_available_fix_when_another_vulnerability_is_unfixable(
    monkeypatch: Any, tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        cobbsagent, "run", lambda *args: (1, _pip_audit_json(*args[:2]), ""),
    )
    findings = cobbsagent.audit(tmp_path / "python", tmp_path)

    assert findings[0]["fix_version"] == "2.1"
    assert findings[0]["unfixable_vuln_ids"] == ["VULN-C"]


def test_audit_failure_is_not_reported_as_no_findings(
    monkeypatch: Any, tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        cobbsagent, "run", lambda *args: (2, "", "temporary resolver failed"),
    )

    findings, succeeded = cobbsagent.audit_result(
        tmp_path / "python", tmp_path,
    )

    assert findings == []
    assert succeeded is False
    with pytest.raises(RuntimeError, match="pip-audit failed"):
        cobbsagent.audit(tmp_path / "python", tmp_path)


def test_sandbox_installs_supported_pip_audit_version(
    monkeypatch: Any, tmp_path: Path,
) -> None:
    commands: list[list[str]] = []

    def fake_run(command, cwd, timeout=900):
        args = [str(part) for part in command]
        commands.append(args)
        if "-m" in args and args[args.index("-m") + 1] == "venv":
            venv = Path(args[-1])
            bindir = "Scripts" if cobbs.sandbox.sys.platform == "win32" else "bin"
            python_name = (
                "python.exe" if cobbs.sandbox.sys.platform == "win32" else "python"
            )
            python = venv / bindir / python_name
            python.parent.mkdir(parents=True, exist_ok=True)
            python.touch()
        return 0, "", ""

    monkeypatch.setattr(cobbs.sandbox, "run", fake_run)
    work = tmp_path / "repo"
    work.mkdir()
    installed, _ = cobbs.sandbox.install(tmp_path / "venv" / "python", work)

    assert installed
    assert len(commands) == 3
    assert "pytest" in commands[0]
    assert "pip-audit" not in commands[0]
    assert "pip-audit>=2.10.1" in commands[2]


def test_audit_uses_isolated_tool_python(
    monkeypatch: Any, tmp_path: Path,
) -> None:
    work = tmp_path / "repo"
    work.mkdir()
    audit_python = cobbs.sandbox._audit_tool_python(work)
    audit_python.parent.mkdir(parents=True)
    audit_python.touch()
    commands: list[list[str]] = []

    def fake_run(command, cwd):
        commands.append([str(part) for part in command])
        return 1, _pip_audit_json(Path(), cwd), ""

    monkeypatch.setattr(cobbsagent, "run", fake_run)
    findings, succeeded = cobbsagent.audit_result(tmp_path / "app-python", work)

    assert succeeded
    assert findings
    assert Path(commands[0][0]) == audit_python


def test_audit_scopes_findings_to_requirements_closure(
    monkeypatch: Any, tmp_path: Path,
) -> None:
    (tmp_path / "requirements.txt").write_text("certifi==2025.1\n", encoding="utf-8")
    closure_result = json.dumps({
        "dependencies": [{
            "name": "certifi", "version": "2025.1",
            "vulns": [{"id": "CERTIFI-VULN", "fix_versions": ["2025.2"]}],
        }],
    })
    whole_environment_result = json.dumps({
        "dependencies": [
            {
                "name": "certifi", "version": "2025.1",
                "vulns": [{"id": "CERTIFI-VULN", "fix_versions": ["2025.2"]}],
            },
            {
                "name": "requests", "version": "2.32.5",
                "vulns": [{"id": "TOOLING-VULN", "fix_versions": ["2.32.6"]}],
            },
        ],
    })
    commands: list[list[str]] = []

    def fake_run(command: list[Any], cwd: Path):
        commands.append([str(part) for part in command])
        output = closure_result if "--requirement" in command else whole_environment_result
        return 1, output, ""

    monkeypatch.setattr(cobbsagent, "run", fake_run)
    findings, succeeded = cobbsagent.audit_result(tmp_path / "python", tmp_path)

    assert succeeded
    assert [finding["name"] for finding in findings] == ["certifi"]
    assert "--requirement" in commands[0]
    requirement_index = commands[0].index("--requirement")
    assert Path(commands[0][requirement_index + 1]) == tmp_path / "requirements.txt"
    assert "--no-deps" not in commands[0]
    assert "requests" not in {finding["name"] for finding in findings}


def test_certifi_safe_reports_only_certifi_with_fake_auditor(
    monkeypatch: Any, tmp_path: Path,
) -> None:
    repo = Path(cobbsagent.__file__).resolve().parent / "eval" / "certifi_safe"
    closure_result = json.dumps({
        "dependencies": [{
            "name": "certifi", "version": "2022.12.7",
            "vulns": [{
                "id": "CERTIFI-VULN", "fix_versions": ["2023.5.7"],
            }],
        }],
    })
    whole_environment_result = json.dumps({
        "dependencies": [
            json.loads(closure_result)["dependencies"][0],
            {
                "name": "requests", "version": "2.32.5",
                "vulns": [{"id": "TOOLING-VULN", "fix_versions": ["2.32.6"]}],
            },
        ],
    })
    audit_commands: list[list[str]] = []

    def fake_make_sandbox(source: Path):
        sandbox = tmp_path / "sandbox"
        work = sandbox / "repo"
        shutil.copytree(source, work)
        return sandbox, work, sandbox / "python"

    def fake_run(command: list[Any], cwd: Path):
        args = [str(part) for part in command]
        audit_commands.append(args)
        output = closure_result if "--requirement" in args else whole_environment_result
        return 1, output, ""

    monkeypatch.setattr(cobbsagent, "make_sandbox", fake_make_sandbox)
    monkeypatch.setattr(cobbsagent, "install", lambda py, work: (True, "installed"))
    monkeypatch.setattr(cobbsagent, "run_tests", lambda py, work: (True, "passed"))
    monkeypatch.setattr(cobbsagent, "run", fake_run)

    findings = eval_harness.eval_repo(repo)

    assert len(findings) == 1
    assert findings[0]["name"] == "certifi"
    assert audit_commands and all("--requirement" in command for command in audit_commands)


def test_eval_harness_reports_audit_failure(
    monkeypatch: Any, tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.setattr(
        eval_harness.m1, "make_sandbox",
        lambda source: (sandbox, repo, tmp_path / "python"),
    )
    monkeypatch.setattr(
        eval_harness.m1, "install", lambda py, work: (True, "installed"),
    )
    monkeypatch.setattr(
        eval_harness.m1, "run_tests", lambda py, work: (True, "passed"),
    )
    monkeypatch.setattr(
        eval_harness.m1, "audit_result", lambda py, work: ([], False),
    )

    assert eval_harness.eval_repo(repo) == [
        {"name": "*", "result": "audit_failed"},
    ]


def test_remaining_vulnerabilities_prevent_clean_proof_when_tests_pass(
    monkeypatch: Any, tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        cobbsagent, "run", lambda *args: (1, _pip_audit_json(*args[:2]), ""),
    )
    attempt: dict[str, Any] = {}
    cobbsagent_m2.record_proof(
        attempt, tmp_path / "python", tmp_path, "pyjwt", tests_passed=True,
    )

    assert attempt["audit_succeeded"] is True
    assert attempt["vulns_remaining"] == ["VULN-A", "VULN-B", "VULN-C"]
    assert attempt["proved_clean"] is False
