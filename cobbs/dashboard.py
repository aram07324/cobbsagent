"""Build a self-contained, offline dashboard from reports and JSONL traces."""

from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any

MAX_TRACE_FILES = 1000
MAX_TRACE_EVENTS = 5000
MAX_TRACE_FILE_BYTES = 2_000_000
SECRET_PATTERNS = (
    re.compile(r"AIza[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{10,}"),
    re.compile(
        r"(?i)\b(?:gemini_api_key|github_token|api[_-]?key|"
        r"access[_-]?token|password|secret)\b\s*[:=]\s*\S+"
    ),
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)\S+"),
)
TRACE_FIELDS = (
    "ts", "event", "step", "name", "tool_name", "backend", "model",
    "status", "cached",
)


def _redact(value: Any) -> str:
    """Redact common credential forms and bound untrusted dashboard values."""
    text = str(value)[:1000]
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def _attempt_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    repositories = report.get("repos", [])
    if not isinstance(repositories, list):
        return rows
    for repository in repositories:
        if not isinstance(repository, dict):
            continue
        repo_name = _redact(Path(str(repository.get("repo", "unknown"))).name)
        attempts = repository.get("attempts", [])
        if not isinstance(attempts, list):
            continue
        for attempt in attempts:
            if not isinstance(attempt, dict):
                continue
            flags = [
                name for name in (
                    "overfit", "injection_followed", "shadowing_suspected",
                    "tampering_suspected",
                ) if attempt.get(name)
            ]
            rows.append({
                "repo": repo_name,
                "package": _redact(attempt.get("name", "unknown")),
                "version": _redact(
                    f"{attempt.get('version', '?')} -> {attempt.get('final_version', '?')}"
                ),
                "category": _redact(attempt.get("category", "unscored")),
                "result": _redact(attempt.get("result", "unknown")),
                "correct": _redact(attempt.get("correct", "n/a")),
                "verified_by": _redact(attempt.get("verified_by", "unknown")),
                "rounds": len(attempt.get("rounds", []))
                if isinstance(attempt.get("rounds"), list) else 0,
                "flags": _redact(", ".join(flags) or "none"),
            })
    return rows


def _trace_rows(trace_dir: Path) -> list[dict[str, str]]:
    if not trace_dir.exists():
        return []
    root = trace_dir.resolve()
    files = sorted(trace_dir.glob("*.jsonl"))
    if len(files) > MAX_TRACE_FILES:
        raise ValueError(f"trace directory exceeds {MAX_TRACE_FILES} files")
    rows: list[dict[str, str]] = []
    for path in files:
        resolved = path.resolve()
        if not resolved.is_relative_to(root):
            raise ValueError(f"trace path escapes its directory: {path.name}")
        if resolved.stat().st_size > MAX_TRACE_FILE_BYTES:
            raise ValueError(f"trace file is too large: {path.name}")
        for line_number, line in enumerate(
            resolved.read_text(encoding="utf-8").splitlines(), start=1,
        ):
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"invalid JSON in {path.name}:{line_number}"
                ) from exc
            if not isinstance(event, dict):
                continue
            rows.append({
                field: _redact(event[field]) if field in event else ""
                for field in TRACE_FIELDS
            })
            if len(rows) >= MAX_TRACE_EVENTS:
                return rows
    return rows


def render_dashboard(
    report: dict[str, Any], trace_rows: list[dict[str, str]],
) -> str:
    """Render a static HTML report; no scripts, external assets, or raw logs."""
    attempts = _attempt_rows(report)
    safe_trace_rows = [{
        field: _redact(event.get(field, ""))
        for field in TRACE_FIELDS
    } for event in trace_rows]
    correct = sum(row["correct"].casefold() == "true" for row in attempts)
    overfit = sum(row["result"] == "overfit" for row in attempts)
    tampering = sum("tampering_suspected" in row["flags"] for row in attempts)
    requests = report.get("requests_used", 0)

    def table(headers: tuple[str, ...], rows: list[dict[str, Any]]) -> str:
        head = "".join(f"<th>{html.escape(label)}</th>" for label in headers)
        body = "".join(
            "<tr>" + "".join(
                f"<td>{html.escape(str(row.get(key, '')))}</td>"
                for key in headers
            ) + "</tr>"
            for row in rows
        )
        return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"

    attempt_headers = (
        "repo", "package", "version", "category", "result", "correct",
        "verified_by", "rounds", "flags",
    )
    trace_headers = tuple(TRACE_FIELDS)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>Dependency Agent Offline Report</title>
<style>
body{{font:15px system-ui,sans-serif;max-width:1200px;margin:2rem auto;padding:0 1rem;color:#17212b}}
h1,h2{{color:#173b57}}.metrics{{display:flex;gap:1rem;flex-wrap:wrap}}
.metric{{padding:.8rem 1rem;background:#eef4f8;border-radius:.5rem}}
table{{border-collapse:collapse;width:100%;margin:1rem 0 2rem;display:block;overflow-x:auto}}
th,td{{border:1px solid #cbd5dc;padding:.45rem .6rem;text-align:left;white-space:nowrap}}
th{{background:#eef4f8}}.note{{color:#4b5c68}}
</style></head><body>
<h1>Dependency Agent Offline Report</h1>
<p class="note">Static, redacted view. Raw source, prompts, tool contents, and test logs are omitted.</p>
<div class="metrics">
<div class="metric">Attempts: {len(attempts)}</div>
<div class="metric">Correct: {correct}</div>
<div class="metric">Overfit: {overfit}</div>
<div class="metric">Tampering signals: {tampering}</div>
<div class="metric">Gemini requests: {html.escape(_redact(requests))}</div>
<div class="metric">Trace events: {len(safe_trace_rows)}</div>
</div>
<h2>Repair attempts</h2>{table(attempt_headers, attempts)}
<h2>Trace events</h2>{table(trace_headers, safe_trace_rows)}
</body></html>"""


def build_dashboard(
    report_path: Path, trace_dir: Path, output_path: Path,
) -> Path:
    """Load bounded local inputs and write a self-contained HTML dashboard."""
    report_data = json.loads(report_path.read_text(encoding="utf-8"))
    if not isinstance(report_data, dict):
        raise ValueError("report must be a JSON object")
    content = render_dashboard(report_data, _trace_rows(trace_dir))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")
    return output_path


def main() -> int:
    """Run the offline dashboard generator."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=Path("report.json"))
    parser.add_argument("--traces", type=Path, default=Path("traces"))
    parser.add_argument("--output", type=Path, default=Path("dashboard.html"))
    args = parser.parse_args()
    try:
        output = build_dashboard(args.report, args.traces, args.output)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(f"Could not build dashboard: {exc}")
    print(f"Offline dashboard written to {output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
