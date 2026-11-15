"""Ollama local HTTP backend."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

from .cache import RateLimitExceeded

class OllamaBackend:
    """Local Ollama chat backend with JSON output and bounded retries."""

    backend_name = "ollama"

    def __init__(
        self, model: str | None = None,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        self.model = model or os.environ.get(
            "COBBSAGENT_OLLAMA_MODEL", "qwen2.5-coder:7b",
        )
        self.sleep_fn = sleep_fn or time.sleep
        self.last_usage: dict[str, int] = {}
        self.total_requests = 0
        self.requests_today = 0

    def __call__(self, system: str, messages: list[dict[str, str]]) -> str:
        self.last_usage = {}
        url = "http://localhost:11434/api/chat"
        request_messages = [{"role": "system", "content": system}]
        request_messages.extend({
            "role": "assistant" if item["role"] == "model" else item["role"],
            "content": item["text"],
        } for item in messages)
        body = {
            "model": self.model,
            "messages": request_messages,
            "stream": False,
            "options": {"temperature": 0, "num_ctx": 8192},
            "format": "json",
        }
        http_retries = 0
        empty_retries = 0
        while True:
            request = urllib.request.Request(
                url, data=json.dumps(body).encode("utf-8"), method="POST",
                headers={"Content-Type": "application/json"},
            )
            self.total_requests += 1
            try:
                with urllib.request.urlopen(request, timeout=90) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                try:
                    response_body = exc.read().decode("utf-8", errors="replace")[:500]
                except OSError:
                    response_body = ""
                if exc.code == 404 and (
                    "not found" in response_body.casefold()
                    or "pull" in response_body.casefold()
                ):
                    raise RuntimeError(
                        f"Ollama model '{self.model}' is not pulled. "
                        f"Run: ollama pull {self.model}"
                    ) from None
                if exc.code in {429, 503} and http_retries < 3:
                    self.sleep_fn(20 * (http_retries + 1))
                    http_retries += 1
                    continue
                if exc.code == 429:
                    raise RateLimitExceeded(
                        "Ollama returned HTTP 429 after all retries."
                    ) from None
                raise RuntimeError(
                    f"Ollama API HTTP {exc.code}: {response_body}"
                ) from None
            except urllib.error.URLError as exc:
                reason = str(exc.reason).casefold()
                if isinstance(exc.reason, (ConnectionRefusedError, TimeoutError)) or (
                    "refused" in reason or "timed out" in reason
                ):
                    raise RuntimeError(
                        "Ollama is not running at http://localhost:11434. "
                        "Start Ollama and pull the configured model."
                    ) from None
                raise RuntimeError(f"Could not reach Ollama: {exc.reason}") from None
            except (json.JSONDecodeError, UnicodeError) as exc:
                raise RuntimeError(f"Ollama returned invalid JSON: {exc}") from None

            self.last_usage = {
                "input_tokens": int(payload.get("prompt_eval_count", 0)),
                "output_tokens": int(payload.get("eval_count", 0)),
            }
            message = payload.get("message")
            text = message.get("content") if isinstance(message, dict) else None
            if isinstance(text, str) and text.strip():
                return text
            if empty_retries < 2:
                self.sleep_fn((2, 5)[empty_retries])
                request_messages.append({
                    "role": "user",
                    "content": "Reply with exactly one JSON object using the command format.",
                })
                body["messages"] = request_messages
                empty_retries += 1
                continue
            raise RuntimeError("Ollama response contained no usable text after retries.")
