"""Pacific-time request budget tracking."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .prompt import PACIFIC

class RequestBudgetExceeded(RuntimeError):
    """Raised before an HTTP request would exceed the daily request budget."""

class DailyRequestCounter:
    """Persist Gemini HTTP attempts for the current America/Los_Angeles date."""

    def __init__(
        self, path: Path = Path("usage_today.json"),
        clock_fn: Callable[[], float] | None = None,
    ) -> None:
        self.path = path
        self.clock_fn = clock_fn or time.time
        self._lock = threading.Lock()
        self._date = ""
        self._count = 0
        self._total_requests = 0
        with self._lock:
            self._refresh()

    def _today(self) -> str:
        return datetime.fromtimestamp(
            self.clock_fn(), timezone.utc,
        ).astimezone(PACIFIC).date().isoformat()

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"date": self._date, "count": self._count}, indent=2) + "\n",
            encoding="utf-8",
        )

    def _refresh(self) -> None:
        today = self._today()
        if self._date == today:
            return
        stored_date = ""
        stored_count = 0
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                stored_date = data["date"]
                stored_count = int(data["count"])
                if not isinstance(stored_date, str) or stored_count < 0:
                    raise ValueError("invalid usage counter data")
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"Could not read request counter {self.path}: {exc}") from exc
        self._date = today
        self._count = stored_count if stored_date == today else 0
        if stored_date and stored_date != today:
            self._write()

    @property
    def requests_today(self) -> int:
        """Return the persisted request count for today's Pacific date."""
        with self._lock:
            self._refresh()
            return self._count

    @property
    def total_requests(self) -> int:
        """Return request attempts counted by this process."""
        with self._lock:
            return self._total_requests

    def ensure_available(self, budget: int) -> None:
        """Raise before the next request would exceed the daily budget."""
        with self._lock:
            self._refresh()
            if self._count >= budget:
                raise RequestBudgetExceeded(
                    f"daily Gemini request budget reached ({self._count}/{budget})"
                )

    def record_attempt(self, budget: int) -> None:
        """Atomically persist one request attempt if budget remains."""
        with self._lock:
            self._refresh()
            if self._count >= budget:
                raise RequestBudgetExceeded(
                    f"daily Gemini request budget reached ({self._count}/{budget})"
                )
            self._count += 1
            self._total_requests += 1
            self._write()
