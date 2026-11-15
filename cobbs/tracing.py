"""Stable JSON-lines tracing for model and tool activity."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

class Trace:
    """Append stable JSONL events to a timestamped trace file."""

    def __init__(self, path: Path | None = None) -> None:
        if path is None:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            path = Path("traces") / f"{stamp}.jsonl"
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, event: str, **fields: Any) -> None:
        """Append one timestamped event with stable JSONL fields."""
        row = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=True) + "\n")
