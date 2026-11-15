"""Command-line interface for milestone two."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .budget import DailyRequestCounter
from .cache import BackendChain, ResponseCache
from .gemini import GeminiBackend
from .m2_metadata import _load_scoring_metadata, _package_key, _repo_key, _backend_requests_today
from .m2_pipeline import run_repository
from .m2_reporting import save_report, save_results_markdown
from .m2_resume import _resume_command, discover_repositories
from .ollama import OllamaBackend
from .sandbox import DOCKER_IMAGE, configured_test_sandbox

def parse_backends(value: str) -> list[tuple[str, str]]:
    """Parse comma-separated backend:model entries, preserving model colons."""
    specs: list[tuple[str, str]] = []
    for item in value.split(","):
        backend, separator, model = item.strip().partition(":")
        backend = backend.casefold()
        model = model.strip()
        if not separator or backend not in {"gemini", "ollama"} or not model:
            raise ValueError(
                f"invalid backend entry {item!r}; expected gemini:model or ollama:model"
            )
        specs.append((backend, model))
    if not specs:
        raise ValueError("at least one backend must be configured")
    return specs

def create_backend_chain(
    value: str, max_requests: int, cache: ResponseCache | None,
    cache_only: bool = False,
) -> BackendChain:
    """Create the configured providers, sharing Gemini's request counter."""
    specs = parse_backends(value)
    counter = DailyRequestCounter() if any(name == "gemini" for name, _ in specs) else None
    backends = []
    for name, model in specs:
        if name == "gemini":
            backends.append(GeminiBackend(
                model=model, max_requests=max_requests, request_counter=counter,
            ))
        else:
            backends.append(OllamaBackend(model=model))
    return BackendChain(backends, cache=cache, cache_only=cache_only)

def main(argv: list[str] | None = None) -> None:
    """Parse CLI options and process each selected repository."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path,
                        help="repository directories or folders containing repositories")
    parser.add_argument("--runs", type=int, default=1,
                        help="independent agent attempts per broken upgrade (default: 1)")
    try:
        default_budget = int(os.environ.get("COBBSAGENT_DAILY_BUDGET", "400"))
    except ValueError:
        parser.error("COBBSAGENT_DAILY_BUDGET must be an integer")
    parser.add_argument(
        "--max-requests", type=int, default=default_budget,
        help="maximum Gemini HTTP attempts for the Pacific day",
    )
    default_backends = os.environ.get("COBBSAGENT_BACKENDS")
    if not default_backends:
        gemini_model = (
            os.environ.get("COBBSAGENT_MODEL")
            or os.environ.get("FREE_MODEL")
            or "gemini-3.5-flash-lite"
        )
        ollama_model = os.environ.get(
            "COBBSAGENT_OLLAMA_MODEL", "qwen2.5-coder:7b",
        )
        default_backends = f"gemini:{gemini_model},ollama:{ollama_model}"
    parser.add_argument(
        "--backends", default=default_backends,
        help="ordered fallback chain, e.g. gemini:model,ollama:model",
    )
    cache_group = parser.add_mutually_exclusive_group()
    cache_group.add_argument(
        "--no-cache", action="store_true", help="disable response-cache reads and writes",
    )
    cache_group.add_argument(
        "--cache-only", action="store_true",
        help="use cached responses only and fail without a network call on a miss",
    )
    parser.add_argument("--resume", action="store_true",
                        help="skip completed repo/package attempts from report.json")
    parser.add_argument(
        "--sandbox", choices=("venv", "docker"), default="venv",
        help="execute pytest in the local venv or an isolated Docker container",
    )
    parser.add_argument(
        "--docker-image",
        default=os.environ.get("COBBSAGENT_DOCKER_IMAGE", DOCKER_IMAGE),
        help="pre-pulled Python image used by --sandbox docker",
    )
    args = parser.parse_args(argv)
    if args.runs < 1:
        parser.error("--runs must be at least 1")
    if args.max_requests < 0:
        parser.error("--max-requests cannot be negative")
    try:
        parse_backends(args.backends)
    except ValueError as exc:
        parser.error(str(exc))
    try:
        repositories = discover_repositories(args.paths)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    if not repositories:
        parser.error("no repository with requirements.txt found")

    prior_by_repo: dict[str, dict[str, Any]] = {}
    if args.resume and Path("report.json").is_file():
        try:
            saved = json.loads(Path("report.json").read_text(encoding="utf-8"))
            prior_by_repo = {
                _repo_key(item["repo"]): item
                for item in saved.get("repos", [])
                if isinstance(item, dict) and isinstance(item.get("repo"), str)
            }
        except (OSError, ValueError, TypeError) as exc:
            parser.error(f"Could not load report.json for resume: {exc}")

    cache = None if args.no_cache else ResponseCache()
    try:
        backend = create_backend_chain(
            args.backends, args.max_requests, cache, cache_only=args.cache_only,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        parser.error(str(exc))
    results: list[dict[str, Any]] = (
        list(prior_by_repo.values()) if args.resume else []
    )
    results_by_repo = {_repo_key(item["repo"]): item for item in results}
    for repo_index, repository in enumerate(repositories):
        print(f"\n==> Auditing {repository}")
        previous = prior_by_repo.get(_repo_key(str(repository)), {})
        scoring_metadata = _load_scoring_metadata(repository)
        try:
            with configured_test_sandbox(args.sandbox, args.docker_image):
                if scoring_metadata is not None:
                    current = run_repository(
                        repository, backend, args.runs,
                        resume_attempts=previous.get("attempts", []),
                        scoring_metadata=scoring_metadata,
                    )
                else:
                    current = run_repository(
                        repository, backend, args.runs,
                        resume_attempts=previous.get("attempts", []),
                    )
        except RuntimeError as exc:
            print(f"Agent error for {repository}: {exc}", file=sys.stderr)
            current = {
                "repo": str(repository), "baseline": None, "attempts": [],
                "error": str(exc),
            }
        old_attempts = previous.get("attempts", [])
        if old_attempts:
            merged = list(old_attempts)
            positions = {
                _package_key(item.get("name", "")): index
                for index, item in enumerate(merged)
                if item.get("name")
            }
            for item in current.get("attempts", []):
                key = _package_key(item.get("name", ""))
                if key in positions:
                    merged[positions[key]] = item
                else:
                    positions[key] = len(merged)
                    merged.append(item)
            current["attempts"] = merged
        key = _repo_key(str(repository))
        if key in results_by_repo:
            results[results.index(results_by_repo[key])] = current
        else:
            results.append(current)
        results_by_repo[key] = current

        if current.get("budget_stopped"):
            unrun = list(current.get("not_run_cases", []))
            unrun.extend(
                {"repo": str(later), "package": None}
                for later in repositories[repo_index + 1:]
            )
            current["not_run_cases"] = unrun
            print("Gemini request budget reached. Cases not run:")
            for case in unrun:
                print(f"  {case.get('repo', repository)} / {case.get('package') or '(not audited)'}")
            print(f"Resume with: {_resume_command(args)}")
            break

    requests_used = _backend_requests_today(backend)
    report = {
        "repos": results, "requests_used": requests_used,
        "max_requests": args.max_requests, "backends": parse_backends(args.backends),
        "cache_enabled": not args.no_cache, "cache_only": args.cache_only,
    }
    if current.get("budget_stopped"):
        report["not_run_cases"] = current.get("not_run_cases", [])
    save_report(report)
    save_results_markdown(results, requests_used, args.max_requests)
