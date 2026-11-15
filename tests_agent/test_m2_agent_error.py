"""Verify an agent failure stops that package but preserves later findings."""

import shutil
from pathlib import Path
from typing import Any

import cobbsagent_m2
from agent import AgentRunError


def test_agent_error_is_recorded_and_later_findings_continue(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "requirements.txt").write_text(
        "first==1\nsecond==1\n", encoding="utf-8",
    )
    sandbox_root = tmp_path / "sandboxes"
    sandbox_root.mkdir()

    def fake_make_sandbox(original: Path):
        sandbox = sandbox_root / f"sandbox-{len(list(sandbox_root.iterdir()))}"
        work = sandbox / "repo"
        shutil.copytree(original, work)
        return sandbox, work, sandbox / "python"

    monkeypatch.setattr(cobbsagent_m2, "make_sandbox", fake_make_sandbox)
    monkeypatch.setattr(cobbsagent_m2, "install", lambda py, repo: (True, "installed"))
    monkeypatch.setattr(
        cobbsagent_m2, "installed_versions",
        lambda py, repo: {
            package: cobbsagent_m2._requirement_version(repo, package)
            for package in ("first", "second")
        },
    )
    test_results = iter([
        (True, "baseline"),
        (False, "break first"), (False, "verify first"),
        (False, "break second"), (False, "verify second"),
    ])
    monkeypatch.setattr(
        cobbsagent_m2, "run_tests", lambda py, repo: next(test_results),
    )

    def fake_audit(py: Path, repo: Path):
        text = (repo / "requirements.txt").read_text(encoding="utf-8")
        findings = []
        for package, vuln in (("first", "CVE-1"), ("second", "CVE-2")):
            version = next(
                line.split("==", 1)[1] for line in text.splitlines()
                if line.startswith(package + "==")
            )
            findings.append({
                "name": package, "version": version, "fix_version": "2",
                "vuln_ids": [vuln, vuln],
            })
        return findings, True

    monkeypatch.setattr(cobbsagent_m2, "audit_result", fake_audit)
    monkeypatch.setattr(
        cobbsagent_m2, "run_agent",
        lambda *args: (_ for _ in ()).throw(
            AgentRunError("fake backend failure", 4, 21, 5),
        ),
    )
    monkeypatch.setattr(cobbsagent_m2, "save_diff", lambda *args: {})
    monkeypatch.setattr(cobbsagent_m2, "Trace", lambda: object())

    report = cobbsagent_m2.run_repository(source, backend=object())

    assert report["baseline"]["tests_passed"] is True
    assert [attempt["result"] for attempt in report["attempts"]] == [
        "agent_failed", "agent_failed",
    ]
    assert all(
        attempt["stop_reason"] == "agent_failed"
        and attempt["proved_clean"] is False
        for attempt in report["attempts"]
    )
    assert report["attempts"][0]["vuln_ids"] == ["CVE-1"]
    assert report["attempts"][0]["steps_used"] == 4
    assert report["attempts"][0]["input_tokens"] == 21
    assert report["attempts"][0]["output_tokens"] == 5
