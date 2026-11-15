"""Behavioral golden test for a fully simulated dependency repair."""

import json
import os
import shutil
from pathlib import Path
from typing import Any

import cobbsagent_m2 as cli

GOLDEN = Path(__file__).parent / "fixtures" / "pipeline_golden.json"


def _simulated_report(tmp_path: Path, monkeypatch: Any) -> dict[str, Any]:
    source = tmp_path / "fake-eval"
    source.mkdir()
    (source / "requirements.txt").write_text("sample==1\n", encoding="utf-8")
    (source / "app.py").write_text("value = 0\n", encoding="utf-8")
    sandbox_root = tmp_path / "sandboxes"
    sandbox_root.mkdir()

    def make_sandbox(repo: Path):
        sandbox = sandbox_root / f"sandbox-{len(list(sandbox_root.iterdir()))}"
        work = sandbox / "repo"
        shutil.copytree(repo, work)
        return sandbox, work, sandbox / "python"

    def audit(py: Path, work: Path):
        version = cli._requirement_version(work, "sample")
        if version == "1":
            return ([{
                "name": "sample", "version": "1", "fix_version": "2",
                "vuln_ids": ["FAKE-ADVISORY"],
            }], True)
        return ([], True)

    tests = iter([
        (True, "baseline"),
        (False, "upgrade broke"),
        (True, "independent verification"),
        (True, "final verification"),
    ])

    class FakeBackend:
        backend_name = "ollama"
        model = "fake-model"

        def __call__(self, system: str, messages: list[dict[str, str]]) -> str:
            raise AssertionError("the fake agent must not contact a backend")

    class FakeTrace:
        path = None

    def fake_agent(
        backend: Any, work: Path, py: Path, package: str, old: str,
        new: str, failing_log: str, trace: Any,
    ) -> dict[str, Any]:
        (work / "app.py").write_text("value = 1\n", encoding="utf-8")
        return {
            "status": "fixed", "summary": "simulated repair",
            "steps_used": 1, "input_tokens": 10, "output_tokens": 5,
        }

    monkeypatch.setattr(cli, "make_sandbox", make_sandbox)
    monkeypatch.setattr(cli, "install", lambda py, work: (True, "installed"))
    monkeypatch.setattr(
        cli, "installed_versions",
        lambda py, work: {
            "sample": cli._requirement_version(work, "sample") or "unknown",
        },
    )
    monkeypatch.setattr(cli, "run_tests", lambda py, work: next(tests))
    monkeypatch.setattr(cli, "audit_result", audit)
    monkeypatch.setattr(cli, "run_agent", fake_agent)
    monkeypatch.setattr(cli, "Trace", FakeTrace)
    monkeypatch.setattr(cli, "save_diff", lambda *args: {})

    return cli.run_repository(
        source, FakeBackend(),
        scoring_metadata={
            "package": "sample", "category": "breaking_easy",
            "agent_expected": "agent_fixed",
        },
    )


def _normalize(report: dict[str, Any]) -> dict[str, Any]:
    normalized = json.loads(json.dumps(report))
    normalized["repo"] = "<repo>"
    for attempt in normalized.get("attempts", []):
        attempt.pop("seconds", None)
        resume_state = attempt.get("resume_state")
        if isinstance(resume_state, dict):
            attempt["resume_state"] = {
                path: content.replace("\r\n", "\n")
                for path, content in resume_state.items()
            }
        for run in attempt.get("runs", []):
            resume_files = run.get("resume_files")
            if isinstance(resume_files, dict):
                run["resume_files"] = {
                    path: content.replace("\r\n", "\n")
                    for path, content in resume_files.items()
                }
    return normalized


def test_full_fake_pipeline_matches_normalized_golden(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    report = _normalize(_simulated_report(tmp_path, monkeypatch))
    if os.environ.get("COBBSAGENT_UPDATE_GOLDEN") == "1":
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    assert report == json.loads(GOLDEN.read_text(encoding="utf-8"))
