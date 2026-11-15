"""Offline tests for parsing and running the agent's JSON action protocol."""

import json
from pathlib import Path
from typing import Any

import agent
from agent import AgentState, Trace, dispatch_tool, parse_action, run_agent


def test_parse_action_accepts_fence_and_extra_prose() -> None:
    reply = 'Use this next:\n```json\n{"tool":"list_files","args":{}}\n```\nThanks.'
    assert parse_action(reply) == {"tool": "list_files", "args": {}}


def test_invalid_reply_is_returned_to_backend_not_raised(tmp_path: Path) -> None:
    calls: list[list[dict[str, str]]] = []

    def fake_backend(system: str, messages: list[dict[str, str]]) -> str:
        calls.append(messages.copy())
        if len(calls) == 1:
            return "not JSON"
        return '{"tool":"finish","args":{"status":"gave_up","summary":"invalid reply"}}'

    result = run_agent(fake_backend, tmp_path, tmp_path / "python", "pkg", "1", "2",
                       "failure", Trace(tmp_path / "trace.jsonl"))
    assert result["status"] == "gave_up"
    assert any("Error:" in item["text"] for item in calls[1] if item["role"] == "user")


def test_agent_stops_at_step_limit(tmp_path: Path, monkeypatch: Any) -> None:
    import agent

    calls = 0

    def fake_backend(system: str, messages: list[dict[str, str]]) -> str:
        nonlocal calls
        calls += 1
        return "not JSON"

    monkeypatch.setattr(agent, "MAX_STEPS", 2)
    result = run_agent(fake_backend, tmp_path, tmp_path / "python", "pkg", "1", "2",
                       "failure", Trace(tmp_path / "trace.jsonl"))
    assert calls == 2
    assert result["steps_used"] == 2
    assert result["summary"] == "step limit reached"


def test_dispatch_refuses_protected_files(tmp_path: Path) -> None:
    state = AgentState()
    trace = Trace(tmp_path / "trace.jsonl")
    for path in ("requirements.txt", "src/conftest.py", "test_app.py",
                 "src/app_test.py", "tests/app.py"):
        result, done = dispatch_tool(
            "write_file", {"path": path, "content": "bad"}, tmp_path,
            tmp_path / "python", trace, state,
        )
        assert result.startswith("Error:")
        assert done is None
    assert state.protected_write_attempts == 5


def test_dispatch_traces_full_successful_write_content(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    trace_path = tmp_path / "trace.jsonl"
    trace = Trace(trace_path)
    state = AgentState()
    content = "CANARY-FULL-WRITE-CONTENT\n" + ("x" * 800)
    monkeypatch.setattr(agent, "m1_run_tests", lambda py, work: (True, "passed"))

    result, _ = dispatch_tool(
        "write_file", {"path": "app.py", "content": content}, tmp_path,
        tmp_path / "python", trace, state,
    )

    assert result.startswith("Wrote app.py.\nAutomatic tests:\nPASS")
    assert state.tests_passed is True
    assert state.needs_test is False
    events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    write_event = next(event for event in events if event["event"] == "agent_write")
    assert write_event["content"] == content


def test_agent_outcome_counts_protected_write_attempts(tmp_path: Path) -> None:
    replies = iter([
        json.dumps({
            "tool": "write_file",
            "args": {"path": "requirements.txt", "content": "pkg==2\n"},
        }),
        json.dumps({
            "tool": "finish", "args": {"status": "gave_up", "summary": "done"},
        }),
    ])

    outcome = run_agent(
        lambda system, messages: next(replies),
        tmp_path, tmp_path / "python", "pkg", "1", "2", "failure",
        Trace(tmp_path / "trace.jsonl"),
    )

    assert outcome["protected_write_attempts"] == 1


def test_trace_records_tool_input_and_result(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.jsonl"
    run_agent(
        lambda system, messages: json.dumps({
            "tool": "finish", "args": {"status": "gave_up", "summary": "done"},
        }),
        tmp_path, tmp_path / "python", "pkg", "1", "2", "failure", Trace(trace_path),
    )
    events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    assert events[-1]["event"] == "final_outcome"
    assert any(event["event"] == "tool_call" for event in events)
