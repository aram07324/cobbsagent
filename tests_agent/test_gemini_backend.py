"""Fake-response coverage for Gemini text extraction and transient retries."""

import io
import json
import re
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

import agent
from agent import GeminiBackend, parse_action


def _response(payload: dict[str, Any]) -> io.BytesIO:
    return io.BytesIO(json.dumps(payload).encode("utf-8"))


def _payload(parts: list[dict[str, Any]], finish: str = "STOP") -> dict[str, Any]:
    return {
        "candidates": [{
            "content": {"parts": parts},
            "finishReason": finish,
        }],
        "usageMetadata": {"promptTokenCount": 11, "candidatesTokenCount": 7},
    }


class _MemoryCounter:
    def __init__(self) -> None:
        self.requests_today = 0
        self.total_requests = 0

    def ensure_available(self, budget: int) -> None:
        if self.requests_today >= budget:
            raise agent.RequestBudgetExceeded("budget reached")

    def record_attempt(self, budget: int) -> None:
        self.ensure_available(budget)
        self.requests_today += 1
        self.total_requests += 1


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _new_backend(
    sleeps: list[float] | None = None, max_requests: int = 400,
) -> tuple[GeminiBackend, _FakeClock]:
    clock = _FakeClock()

    def fake_sleep(seconds: float) -> None:
        clock.sleep(seconds)
        if sleeps is not None:
            sleeps.append(seconds)

    return GeminiBackend(
        sleep_fn=fake_sleep, clock_fn=clock, rpm=12,
        max_requests=max_requests, request_counter=_MemoryCounter(),
    ), clock


def _call_backend(
    monkeypatch: pytest.MonkeyPatch,
    outcomes: list[Any],
    sleeps: list[float] | None = None,
) -> tuple[str, GeminiBackend, list[Any]]:
    calls: list[Any] = []
    pending = iter(outcomes)

    def fake_urlopen(request: Any, timeout: int) -> io.BytesIO:
        calls.append(request)
        outcome = next(pending)
        if isinstance(outcome, BaseException):
            raise outcome
        return _response(outcome)

    monkeypatch.setenv("GEMINI_API_KEY", "not-a-real-test-key")
    monkeypatch.setattr(agent.urllib.request, "urlopen", fake_urlopen)
    backend, _ = _new_backend(sleeps)
    result = backend("system", [{"role": "user", "text": "prompt"}])
    return result, backend, calls


@pytest.mark.parametrize(
    ("parts", "expected"),
    [
        ([{"text": "answer", "thoughtSignature": "signed"}], "answer"),
        ([{"text": "internal", "thought": True}, {"text": "answer"}], "answer"),
        ([{"text": "first "}, {"text": "second"}], "first second"),
    ],
)
def test_extracts_candidate_text(
    monkeypatch: pytest.MonkeyPatch, parts: list[dict[str, Any]], expected: str,
) -> None:
    result, backend, _ = _call_backend(monkeypatch, [_payload(parts)])
    assert result == expected
    assert backend.last_usage == {"input_tokens": 11, "output_tokens": 7}


def test_function_call_part_falls_back_to_json_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _payload([{
        "functionCall": {"name": "list_files", "args": {}},
        "thoughtSignature": "signature",
    }])
    result, _, _ = _call_backend(monkeypatch, [payload])
    assert parse_action(result) == {"tool": "list_files", "args": {}}


def test_text_reply_is_returned_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    text = 'prose {"tool":"finish","args":{"status":"gave_up","summary":"ok"}}'
    result, _, _ = _call_backend(monkeypatch, [_payload([{"text": text}])])
    assert result == text


def test_system_prompt_avoids_native_calling_terminology() -> None:
    assert not re.search(r"\b(tool|function|call)\b", agent.SYSTEM_PROMPT, re.IGNORECASE)


def test_empty_max_tokens_response_retries_then_explains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    empty = _payload([], "MAX_TOKENS")
    monkeypatch.setenv("GEMINI_API_KEY", "not-a-real-test-key")
    calls = 0

    def fake_urlopen(request: Any, timeout: int) -> io.BytesIO:
        nonlocal calls
        calls += 1
        return _response(empty)

    monkeypatch.setattr(agent.urllib.request, "urlopen", fake_urlopen)
    backend, _ = _new_backend(sleeps)
    with pytest.raises(RuntimeError, match="MAX_TOKENS"):
        backend("system", [{"role": "user", "text": "prompt"}])
    assert calls == 3
    assert sleeps == [2, 3, 5]


def test_missing_candidates_reports_prompt_feedback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {"candidates": [], "promptFeedback": {"blockReason": "SAFETY"}}
    monkeypatch.setenv("GEMINI_API_KEY", "not-a-real-test-key")
    monkeypatch.setattr(
        agent.urllib.request, "urlopen", lambda request, timeout: _response(payload),
    )
    backend, _ = _new_backend()
    with pytest.raises(RuntimeError, match="promptFeedback=.*SAFETY"):
        backend("system", [{"role": "user", "text": "prompt"}])


def test_empty_response_then_valid_response_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    result, _, calls = _call_backend(
        monkeypatch, [_payload([], "MAX_TOKENS"), _payload([{"text": "ok"}])], sleeps,
    )
    assert result == "ok"
    assert len(calls) == 2
    assert sleeps == [2, 3]
    request_body = json.loads(calls[0].data.decode("utf-8"))
    assert request_body["generationConfig"] == {
        "temperature": 0, "maxOutputTokens": 4096,
        "responseMimeType": "application/json",
    }

    def contains_forbidden(value: Any) -> bool:
        if isinstance(value, dict):
            return any(
                key in {"tools", "functionDeclarations", "toolConfig"}
                or contains_forbidden(item)
                for key, item in value.items()
            )
        if isinstance(value, list):
            return any(contains_forbidden(item) for item in value)
        return False

    assert not contains_forbidden(request_body)
    assert "function_calling_config" not in json.dumps(request_body)


def test_pacing_separates_requests_and_budget_counts_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payloads = iter([_payload([{"text": "one"}]), _payload([{"text": "two"}])])
    clock_values: list[float] = []
    backend, clock = _new_backend(max_requests=2)

    def fake_urlopen(request: Any, timeout: int) -> io.BytesIO:
        clock_values.append(clock())
        return _response(next(payloads))

    monkeypatch.setenv("GEMINI_API_KEY", "not-a-real-test-key")
    monkeypatch.setattr(agent.urllib.request, "urlopen", fake_urlopen)
    prompt = [{"role": "user", "text": "prompt"}]
    assert backend("system", prompt) == "one"
    assert backend("system", prompt) == "two"
    assert clock_values[1] - clock_values[0] >= 5
    assert backend.total_requests == 2
    assert backend.requests_today == 2

    limited, _ = _new_backend(max_requests=1)
    calls = 0

    def one_response(request: Any, timeout: int) -> io.BytesIO:
        nonlocal calls
        calls += 1
        return _response(_payload([{"text": "ok"}]))

    monkeypatch.setattr(agent.urllib.request, "urlopen", one_response)
    limited("system", prompt)
    with pytest.raises(agent.RequestBudgetExceeded):
        limited("system", prompt)
    assert calls == 1
    assert limited.total_requests == 1


def test_daily_counter_resets_on_pacific_date_change(tmp_path: Path) -> None:
    timestamp = [datetime(2026, 1, 2, 7, 59, tzinfo=timezone.utc).timestamp()]
    counter_path = tmp_path / "usage_today.json"
    counter = agent.DailyRequestCounter(counter_path, clock_fn=lambda: timestamp[0])

    assert counter.requests_today == 0
    counter.record_attempt(10)
    assert counter.requests_today == 1
    assert json.loads(counter_path.read_text(encoding="utf-8")) == {
        "date": "2026-01-01", "count": 1,
    }

    timestamp[0] = datetime(2026, 1, 2, 8, 1, tzinfo=timezone.utc).timestamp()
    assert counter.requests_today == 0
    assert json.loads(counter_path.read_text(encoding="utf-8")) == {
        "date": "2026-01-02", "count": 0,
    }
    counter.record_attempt(10)
    assert counter.requests_today == 1


def test_malformed_reply_retries_then_accepts_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    malformed = {
        "candidates": [{
            "content": {"parts": []}, "finishReason": "MALFORMED_FUNCTION_CALL",
        }],
    }
    valid_text = '{"command":"finish","args":{"status":"gave_up","summary":"ok"}}'
    result, _, calls = _call_backend(
        monkeypatch, [malformed, _payload([{"text": valid_text}])], sleeps,
    )
    assert result == valid_text
    assert len(calls) == 2
    assert sleeps == [2, 3]


def test_malformed_reply_retries_and_salvages_finish_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    malformed = {
        "candidates": [{
            "content": {"parts": []},
            "finishReason": "MALFORMED_FUNCTION_CALL",
            "finishMessage": "Malformed function call: call:list_files({})",
        }],
    }
    result, _, calls = _call_backend(
        monkeypatch, [malformed, malformed, malformed], sleeps,
    )
    assert parse_action(result) == {"tool": "list_files", "args": {}}
    assert len(calls) == 3
    assert sleeps == [2, 3, 5]


def test_retry_request_appends_plain_text_correction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    malformed = {
        "candidates": [{
            "content": {"parts": []}, "finishReason": "MALFORMED_FUNCTION_CALL",
        }],
    }
    valid = _payload([{"text": '{"command":"list_files","args":{}}'}])
    _, _, calls = _call_backend(monkeypatch, [malformed, valid])
    retry_body = json.loads(calls[1].data.decode("utf-8"))
    last_turn = retry_body["contents"][-1]
    assert last_turn == {
        "role": "user",
        "parts": [{
            "text": "Reply with exactly one JSON object using the command format. "
            "Do not use function calling.",
        }],
    }


def test_http_503_retries_but_404_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    transient = [
        urllib.error.HTTPError("https://example.invalid", 503, "busy", {}, io.BytesIO(b"busy")),
        urllib.error.HTTPError("https://example.invalid", 503, "busy", {}, io.BytesIO(b"busy")),
        _payload([{"text": "recovered"}]),
    ]
    result, backend, calls = _call_backend(monkeypatch, transient, sleeps)
    assert result == "recovered"
    assert len(calls) == 3
    assert sleeps == [20, 40]
    assert backend.total_requests == 3

    errors: list[Any] = []

    def fake_404(request: Any, timeout: int) -> io.BytesIO:
        errors.append(request)
        raise urllib.error.HTTPError(
            request.full_url, 404, "missing", {}, io.BytesIO(b"model not found"),
        )

    monkeypatch.setattr(agent.urllib.request, "urlopen", fake_404)
    backend, _ = _new_backend(sleeps)
    with pytest.raises(
        RuntimeError,
        match=rf"HTTP 404.*v1beta/models/{re.escape(backend.model)}:generateContent",
    ):
        backend("system", [{"role": "user", "text": "prompt"}])
    assert len(errors) == 1
    assert backend.total_requests == 1
    assert sleeps == [20, 40]


def test_http_429_is_signaled_only_after_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    calls = 0

    def fake_429(request: Any, timeout: int) -> io.BytesIO:
        nonlocal calls
        calls += 1
        raise urllib.error.HTTPError(
            request.full_url, 429, "busy", {}, io.BytesIO(b"busy"),
        )

    monkeypatch.setenv("GEMINI_API_KEY", "not-a-real-test-key")
    monkeypatch.setattr(agent.urllib.request, "urlopen", fake_429)
    backend, _ = _new_backend(sleeps)

    with pytest.raises(agent.RateLimitExceeded, match="after all retries"):
        backend("system", [{"role": "user", "text": "prompt"}])

    assert calls == 4
    assert sleeps == [20, 40, 60]
    assert backend.total_requests == 4
