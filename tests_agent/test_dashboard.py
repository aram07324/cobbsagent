"""Offline dashboard tests use synthetic, non-secret report and trace data."""

import json
from pathlib import Path

import demo
import cobbsagent_m2
from cobbs.dashboard import build_dashboard, render_dashboard


def test_dashboard_is_static_escaped_redacted_and_omits_raw_logs(
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "report.json"
    trace_dir = tmp_path / "traces"
    trace_dir.mkdir()
    report_path.write_text(json.dumps({
        "requests_used": 3,
        "repos": [{
            "repo": "C:\\private\\eval\\demo",
            "attempts": [{
                "name": "<script>alert(1)</script>",
                "version": "1.0", "final_version": "2.0",
                "category": "breaking", "result": "agent_fixed",
                "correct": True, "verified_by": "hidden_test",
                "rounds": [{"round": 1}],
                "log": "AIza" + "A" * 32,
            }],
        }],
    }), encoding="utf-8")
    (trace_dir / "trace.jsonl").write_text(json.dumps({
        "ts": "2025-01-01T00:00:00Z",
        "event": "tool_result",
        "tool_name": "write_file",
        "model": "<img src=x onerror=alert(1)>",
        "result": "ghp_" + "B" * 30,
        "input": "this raw prompt must not be shown",
    }) + "\n", encoding="utf-8")

    output = build_dashboard(
        report_path, trace_dir, tmp_path / "dashboard.html",
    ).read_text(encoding="utf-8")
    assert "<!doctype html>" in output
    assert "<script>" not in output and "<img src=x" not in output
    assert "&lt;script&gt;" in output and "&lt;img" in output
    assert "this raw prompt must not be shown" not in output
    assert "[REDACTED]" not in output  # raw log and raw tool result were omitted
    assert "AIza" not in output and "ghp_" not in output
    assert "https://" not in output
    assert "default-src 'none'" in output


def test_trace_fields_are_redacted_and_rendered_without_html_injection() -> None:
    html = render_dashboard({}, [{
        "ts": "now", "event": "agent_write",
        "model": "github_token=offline-test-value <b>unsafe</b>",
    }])
    assert "github_token=offline-test-value" not in html
    assert "&lt;b&gt;unsafe&lt;/b&gt;" in html
    assert "<b>unsafe</b>" not in html


def test_invalid_trace_json_fails_explicitly(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    report.write_text("{}", encoding="utf-8")
    traces = tmp_path / "traces"
    traces.mkdir()
    (traces / "broken.jsonl").write_text("{not-json}\n", encoding="utf-8")
    try:
        build_dashboard(report, traces, tmp_path / "dashboard.html")
    except ValueError as exc:
        assert "broken.jsonl:1" in str(exc)
    else:
        raise AssertionError("invalid trace input should be reported")


def test_demo_runs_repairs_before_rendering(monkeypatch, tmp_path: Path) -> None:
    calls = []

    def fake_run_repairs(repo: Path, pipeline_args: list[str]) -> None:
        calls.append(("repairs", repo, pipeline_args))

    def fake_build_dashboard(
        report: Path, traces: Path, output: Path,
    ) -> Path:
        calls.append(("dashboard", report, traces, output))
        return output

    monkeypatch.setattr(demo, "run_repairs", fake_run_repairs)
    monkeypatch.setattr(demo, "build_dashboard", fake_build_dashboard)
    repo = tmp_path / "repo"
    output = tmp_path / "dashboard.html"

    assert demo.main([
        "--repo", str(repo), "--output", str(output),
        "--runs", "2", "--backends", "ollama:test-model",
    ]) == 0

    assert calls == [
        ("repairs", repo, ["--runs", "2", "--backends", "ollama:test-model"]),
        ("dashboard", Path("report.json"), Path("traces"), output),
    ]


def test_demo_forwards_repair_options_to_milestone_two(
    monkeypatch, tmp_path: Path,
) -> None:
    calls = []
    monkeypatch.setattr(
        cobbsagent_m2, "main",
        lambda argv: calls.append(argv),
    )

    demo.run_repairs(tmp_path / "repo", ["--runs", "2"])

    assert calls == [[str(tmp_path / "repo"), "--runs", "2"]]


def test_demo_dashboard_only_does_not_run_repairs(
    monkeypatch, tmp_path: Path,
) -> None:
    def unexpected_run(*args) -> None:
        raise AssertionError("dashboard-only mode must not run repairs")

    monkeypatch.setattr(demo, "run_repairs", unexpected_run)
    monkeypatch.setattr(
        demo, "build_dashboard", lambda report, traces, output: output,
    )

    assert demo.main([
        "--dashboard-only", "--report", str(tmp_path / "report.json"),
        "--traces", str(tmp_path / "traces"),
        "--output", str(tmp_path / "dashboard.html"),
    ]) == 0
