"""Response caching and per-attempt backend selection."""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any, Callable

class CacheMissError(RuntimeError):
    """Raised when cache-only mode cannot satisfy a response."""

class RateLimitExceeded(RuntimeError):
    """Raised after a backend exhausts retries for HTTP 429."""

class ResponseCache:
    """Append-only JSONL cache for backend responses."""

    def __init__(self, path: Path = Path("cache") / "responses.jsonl") -> None:
        self.path = path
        self._lock = threading.Lock()
        self._entries: dict[str, dict[str, Any]] | None = None

    @staticmethod
    def make_key(
        backend: str, model: str, system: str, messages: list[dict[str, str]],
        run_index: int,
    ) -> str:
        """Hash the provider, prompt, messages, and independent run index."""
        material = json.dumps(
            [backend, model, system, messages, run_index],
            sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _load(self) -> dict[str, dict[str, Any]]:
        entries: dict[str, dict[str, Any]] = {}
        if not self.path.exists():
            return entries
        try:
            with self.path.open(encoding="utf-8") as stream:
                for line_number, line in enumerate(stream, 1):
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    if (
                        not isinstance(row, dict)
                        or not isinstance(row.get("key"), str)
                        or not isinstance(row.get("response"), str)
                        or not isinstance(row.get("usage", {}), dict)
                    ):
                        raise ValueError(f"invalid cache entry on line {line_number}")
                    entries[row["key"]] = row
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise RuntimeError(f"Could not read response cache {self.path}: {exc}") from exc
        return entries

    def get(self, key: str) -> dict[str, Any] | None:
        """Return a cached response entry, if present."""
        with self._lock:
            if self._entries is None:
                self._entries = self._load()
            entry = self._entries.get(key)
            return dict(entry) if entry is not None else None

    def put(self, key: str, response: str, usage: dict[str, int]) -> None:
        """Append a response entry and update the in-memory index."""
        row = {"key": key, "response": response, "usage": usage}
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with self.path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(row, ensure_ascii=True) + "\n")
            except OSError as exc:
                raise RuntimeError(f"Could not write response cache {self.path}: {exc}") from exc
            if self._entries is None:
                self._entries = {}
            self._entries[key] = row

class CachedBackend:
    """Bind a provider to one independent run index and optional response cache."""

    def __init__(
        self, provider: Callable[[str, list[dict[str, str]]], str],
        cache: ResponseCache | None, run_index: int, cache_only: bool = False,
    ) -> None:
        self.provider = provider
        self.backend_name = str(getattr(provider, "backend_name", "custom"))
        self.model = str(getattr(provider, "model", ""))
        self.cache = cache
        self.run_index = run_index
        self.cache_only = cache_only
        self.agent_requests = 0
        self.cache_hits = 0
        self.last_cached = False
        self.last_usage: dict[str, int] = {}

    @property
    def total_requests(self) -> int:
        """Return this provider's total network-attempt count."""
        return int(getattr(self.provider, "total_requests", 0))

    @property
    def requests_today(self) -> int:
        """Return this provider's Pacific-day request count."""
        return int(getattr(self.provider, "requests_today", 0))

    def __call__(self, system: str, messages: list[dict[str, str]]) -> str:
        self.last_cached = False
        key = ResponseCache.make_key(
            self.backend_name, self.model, system, messages, self.run_index,
        )
        cached = self.cache.get(key) if self.cache is not None else None
        if cached is not None:
            self.cache_hits += 1
            self.last_cached = True
            self.last_usage = {"input_tokens": 0, "output_tokens": 0}
            return cached["response"]
        if self.cache_only:
            raise CacheMissError(
                f"response cache miss for {self.backend_name}/{self.model}, "
                f"run {self.run_index}"
            )
        self.agent_requests += 1
        try:
            response = self.provider(system, messages)
        except Exception:
            self._capture_usage()
            raise
        self._capture_usage()
        if self.cache is not None:
            self.cache.put(key, response, self.last_usage)
        return response

    def _capture_usage(self) -> None:
        usage = getattr(self.provider, "last_usage", {})
        self.last_usage = {
            "input_tokens": int(usage.get("input_tokens", 0)),
            "output_tokens": int(usage.get("output_tokens", 0)),
        } if isinstance(usage, dict) else {"input_tokens": 0, "output_tokens": 0}

class BackendChain:
    """Select a backend per repair attempt and advance only after eligible failures."""

    def __init__(
        self,
        backends: list[Callable[[str, list[dict[str, str]]], str]],
        cache: ResponseCache | None = None, cache_only: bool = False,
    ) -> None:
        if not backends:
            raise ValueError("at least one backend is required")
        self.backends = backends
        self.cache = cache
        self.cache_only = cache_only
        self.index = 0

    @property
    def current(self) -> Callable[[str, list[dict[str, str]]], str]:
        """Return the provider selected for the current attempt."""
        return self.backends[self.index]

    def for_attempt(self, run_index: int) -> CachedBackend:
        """Bind the selected provider to the cache and run index."""
        return CachedBackend(self.current, self.cache, run_index, self.cache_only)

    def advance(self) -> bool:
        """Switch to the next provider, returning false at the end of the chain."""
        if self.index + 1 >= len(self.backends):
            return False
        self.index += 1
        return True

    @property
    def total_requests(self) -> int:
        """Return all provider request attempts."""
        return sum(int(getattr(backend, "total_requests", 0)) for backend in self.backends)

    @property
    def gemini_total_requests(self) -> int:
        """Return unique Gemini counter totals across the configured chain."""
        total = 0
        counters: set[int] = set()
        for backend in self.backends:
            if getattr(backend, "backend_name", "") != "gemini":
                continue
            counter = getattr(backend, "request_counter", backend)
            if id(counter) not in counters:
                counters.add(id(counter))
                total += int(getattr(backend, "total_requests", 0))
        return total

    @property
    def requests_today(self) -> int:
        """Return unique Gemini request counts for the current Pacific day."""
        total = 0
        counters: set[int] = set()
        for backend in self.backends:
            if getattr(backend, "backend_name", "") != "gemini":
                continue
            counter = getattr(backend, "request_counter", backend)
            if id(counter) not in counters:
                counters.add(id(counter))
                total += int(getattr(backend, "requests_today", 0))
        return total
