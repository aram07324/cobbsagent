"""Bounded agent loop for the repository repair protocol."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from .budget import RequestBudgetExceeded
from .cache import RateLimitExceeded
from .prompt import MAX_INPUT_TOKENS_TOTAL, MAX_STEPS, SYSTEM_PROMPT
from .safety import AgentState, build_task_prompt, dispatch_tool
from .tracing import Trace

def fallback_reason(error: BaseException) -> str | None:
    """Return whether an exhausted backend may be replaced at an attempt boundary."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, RequestBudgetExceeded):
            return "daily_budget"
        if isinstance(current, RateLimitExceeded):
            return "rate_limited"
        current = current.__cause__ or current.__context__
    return None

def parse_action(text: str) -> dict[str, Any]:
    """Extract and validate a JSON command, allowing prior tool-key responses."""
    decoder = json.JSONDecoder()
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        tool = value.get("command", value.get("tool"))
        args = value.get("args")
        if tool not in {"list_files", "read_file", "write_file", "run_tests", "finish"}:
            raise ValueError(f"Unknown action tool: {tool!r}")
        if not isinstance(args, dict):
            raise ValueError("Action args must be a JSON object.")
        return {"tool": tool, "args": args}
    raise ValueError("Model reply did not contain a valid JSON action object.")

class AgentRunError(RuntimeError):
    """Backend failure annotated with progress already consumed by the agent."""

    def __init__(
        self, message: str, steps_used: int, input_tokens: int, output_tokens: int,
        protected_write_attempts: int = 0,
    ) -> None:
        super().__init__(message)
        self.steps_used = steps_used
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.protected_write_attempts = protected_write_attempts

def run_agent(
    backend: Callable[[str, list[dict[str, str]]], str], work: Path, py: Path,
    package: str, old_ver: str, new_ver: str, failing_log: str, trace: Trace,
) -> dict[str, Any]:
    """Run the bounded plain-text JSON action protocol using an injectable backend."""
    messages = [{
        "role": "user",
        "text": build_task_prompt(package, old_ver, new_ver, failing_log, work),
    }]
    state = AgentState()
    total_in = 0
    total_out = 0

    def finish(status: str, summary: str) -> dict[str, Any]:
        """Log and return a completed agent outcome."""
        outcome = {
            "status": status, "summary": summary, "steps_used": state.steps_used,
            "input_tokens": total_in, "output_tokens": total_out,
            "protected_write_attempts": state.protected_write_attempts,
        }
        trace.log("final_outcome", **outcome)
        return outcome

    for step in range(MAX_STEPS):
        state.steps_used += 1
        try:
            response_text = backend(SYSTEM_PROMPT, messages)
        except Exception as exc:
            usage = getattr(backend, "last_usage", {})
            prompt_tokens = int(usage.get("input_tokens", 0)) if isinstance(usage, dict) else 0
            candidate_tokens = (
                int(usage.get("output_tokens", 0)) if isinstance(usage, dict) else 0
            )
            total_in += prompt_tokens
            total_out += candidate_tokens
            trace.log("model_error", step=step, error=type(exc).__name__, usage={
                "promptTokenCount": prompt_tokens,
                "candidatesTokenCount": candidate_tokens,
            })
            trace.log("final_outcome", status="agent_error", summary=type(exc).__name__,
                      steps_used=state.steps_used, input_tokens=total_in,
                      output_tokens=total_out)
            raise AgentRunError(
                str(exc), state.steps_used, total_in, total_out,
                state.protected_write_attempts,
            ) from exc
        usage = getattr(backend, "last_usage", {})
        prompt_tokens = int(usage.get("input_tokens", 0)) if isinstance(usage, dict) else 0
        candidate_tokens = (
            int(usage.get("output_tokens", 0)) if isinstance(usage, dict) else 0
        )
        total_in += prompt_tokens
        total_out += candidate_tokens
        trace.log("model_response", step=step, usage={
            "promptTokenCount": prompt_tokens,
            "candidatesTokenCount": candidate_tokens,
        }, cached=bool(getattr(backend, "last_cached", False)))
        if total_in > MAX_INPUT_TOKENS_TOTAL:
            return finish("gave_up", "token budget exceeded")
        messages.append({"role": "model", "text": response_text})
        try:
            action = parse_action(response_text)
        except ValueError as exc:
            result = f"Error: {exc} Return one JSON action object."
            trace.log("tool_call", name="parse_action",
                      input=response_text[:500], result=result[:500])
            messages.append({"role": "user", "text": result})
            continue

        result, done = dispatch_tool(
            action["tool"], action["args"], work, py, trace, state,
        )
        if done is not None:
            return finish(done["status"], done["summary"])
        messages.append({"role": "user", "text": result})
    return finish("gave_up", "step limit reached")
