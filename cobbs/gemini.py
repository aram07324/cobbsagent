"""Gemini HTTP backend with pacing, retries, and budget accounting."""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

from .budget import DailyRequestCounter
from .cache import RateLimitExceeded
from .prompt import MODEL

class GeminiBackend:
    """Callable Gemini backend; token usage stays local to this instance."""

    backend_name = "gemini"

    def __init__(
        self,
        model: str = MODEL,
        sleep_fn: Callable[[float], None] | None = None,
        clock_fn: Callable[[], float] | None = None,
        rpm: float | None = None,
        max_requests: int | None = None,
        request_counter: DailyRequestCounter | None = None,
    ) -> None:
        self.model = model
        self.sleep_fn = sleep_fn or time.sleep
        self.clock_fn = clock_fn or time.monotonic
        rpm_value = rpm if rpm is not None else float(os.environ.get("COBBSAGENT_RPM", "12"))
        if rpm_value <= 0:
            raise ValueError("COBBSAGENT_RPM must be greater than zero")
        self.minimum_interval = 60.0 / rpm_value
        budget_value = (
            max_requests if max_requests is not None
            else int(os.environ.get("COBBSAGENT_DAILY_BUDGET", "400"))
        )
        if budget_value < 0:
            raise ValueError("daily request budget cannot be negative")
        self.max_requests = budget_value
        self.request_counter = request_counter or DailyRequestCounter()
        self._last_request_at: float | None = None
        self._pacing_lock = threading.Lock()
        self.last_usage: dict[str, int] = {}

    @property
    def requests_today(self) -> int:
        """Return the current Pacific-day request count."""
        return self.request_counter.requests_today

    @property
    def total_requests(self) -> int:
        """Return all Gemini HTTP attempts counted by this backend."""
        return self.request_counter.total_requests

    def _pace(self) -> None:
        with self._pacing_lock:
            now = self.clock_fn()
            if self._last_request_at is not None:
                delay = self.minimum_interval - (now - self._last_request_at)
                if delay > 0:
                    self.sleep_fn(delay)
                    now = self.clock_fn()
                    if now - self._last_request_at < self.minimum_interval:
                        now = self._last_request_at + self.minimum_interval
            self._last_request_at = now

    def __call__(self, system: str, messages: list[dict[str, str]]) -> str:
        """POST one GenerateContent request and return its first text part."""
        self.last_usage = {}
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "GEMINI_API_KEY is missing. Create a free key at aistudio.google.com "
                "and set the GEMINI_API_KEY environment variable."
            )
        model = urllib.parse.quote(self.model, safe=".-")
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent"
        )
        path = urllib.parse.urlsplit(url).path
        http_retries = 0
        empty_retries = 0
        request_messages = [dict(message) for message in messages]
        while True:
            body = {
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [
                    {"role": message["role"],
                     "parts": [{"text": message["text"]}]}
                    for message in request_messages
                ],
                "generationConfig": {
                    "temperature": 0,
                    "maxOutputTokens": 4096,
                    "responseMimeType": "application/json",
                },
            }
            request = urllib.request.Request(
                url, data=json.dumps(body).encode("utf-8"), method="POST",
                headers={
                    "Content-Type": "application/json",
                    "x-goog-api-key": api_key,
                },
            )
            self.request_counter.ensure_available(self.max_requests)
            self._pace()
            self.request_counter.record_attempt(self.max_requests)
            try:
                with urllib.request.urlopen(request, timeout=90) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code in {429, 503} and http_retries < 3:
                    self.sleep_fn(20 * (http_retries + 1))
                    http_retries += 1
                    continue
                if exc.code == 429:
                    raise RateLimitExceeded(
                        "Gemini API returned HTTP 429 after all retries."
                    ) from None
                try:
                    response_body = exc.read().decode("utf-8", errors="replace")[:300]
                except OSError:
                    response_body = ""
                if api_key:
                    response_body = response_body.replace(api_key, "[REDACTED]")
                raise RuntimeError(
                    f"Gemini API HTTP {exc.code} for {path}: {response_body}"
                ) from None
            except urllib.error.URLError as exc:
                raise RuntimeError(f"Could not reach Gemini API: {exc.reason}") from None

            usage = payload.get("usageMetadata", {})
            self.last_usage = {
                "input_tokens": int(usage.get("promptTokenCount", 0)),
                "output_tokens": int(usage.get("candidatesTokenCount", 0)),
            }
            candidates = payload.get("candidates") or []
            candidate = candidates[0] if candidates else {}
            parts = (candidate.get("content") or {}).get("parts") or []
            finish_reason = candidate.get("finishReason", payload.get("finishReason"))
            finish_message = candidate.get("finishMessage", payload.get("finishMessage"))
            text = "".join(
                part["text"] for part in parts
                if isinstance(part, dict)
                and isinstance(part.get("text"), str)
                and part["text"]
                and part.get("thought") is not True
            )
            malformed = finish_reason == "MALFORMED_FUNCTION_CALL"
            if text and not malformed:
                return text
            function_call = next(
                (part["functionCall"] for part in parts
                 if isinstance(part, dict) and isinstance(part.get("functionCall"), dict)),
                None,
            )
            retryable = malformed or not text and not function_call
            if retryable and empty_retries < 2:
                self.sleep_fn((2, 5)[empty_retries])
                request_messages.append({
                    "role": "user",
                    "text": "Reply with exactly one JSON object using the command format. "
                    "Do not use function calling.",
                })
                empty_retries += 1
                continue
            if function_call and (not retryable or empty_retries >= 2):
                return json.dumps({
                    "command": function_call.get("name"),
                    "args": function_call.get("args", {}),
                })
            if malformed and isinstance(finish_message, str):
                match = re.search(
                    r"\bcall:([A-Za-z_][A-Za-z0-9_]*)\((.*)\)", finish_message,
                )
                if match and match.group(1) in {
                    "list_files", "read_file", "write_file", "run_tests", "finish",
                }:
                    try:
                        args = json.loads(match.group(2))
                    except json.JSONDecodeError:
                        args = {}
                    if not isinstance(args, dict):
                        args = {}
                    return json.dumps({"command": match.group(1), "args": args})
            details = json.dumps(payload)[:400]
            prompt_feedback = (
                f", promptFeedback={payload.get('promptFeedback')!r}"
                if not candidates else ""
            )
            raise RuntimeError(
                "Gemini response contained no usable text; "
                f"finishReason={finish_reason!r}, finishMessage={finish_message!r}, "
                f"parts={len(parts)}{prompt_feedback}, response={details}"
            )
