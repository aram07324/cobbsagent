"""Prepare an allow-listed dependency-fix pull request; dry-run by default."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any, Callable, Sequence
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parent
REPO_URL_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SECRET_RE = re.compile(r"AIza|ghp_|github_pat_", re.I)


class PRWorkflowError(RuntimeError):
    """Raised when an unsafe or incomplete pull-request workflow is detected."""


def parse_repo_url(value: str) -> tuple[str, str, str]:
    """Validate a canonical GitHub repository URL and return owner, name, URL."""
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https" or parsed.netloc != "github.com"
        or parsed.query or parsed.fragment or value.endswith(".")
    ):
        raise ValueError("repo URL must be a canonical https://github.com/owner/name URL")
    parts = parsed.path.removeprefix("/").split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError("repo URL must contain exactly an owner and repository")
    owner, name = parts
    if name.endswith(".git"):
        name = name[:-4]
    if (
        not REPO_URL_RE.fullmatch(f"{owner}/{name}")
        or owner.endswith(".") or name.endswith(".")
    ):
        raise ValueError("repo URL contains an invalid owner or repository name")
    return owner, name, f"https://github.com/{owner}/{name}.git"


def _allowed_repositories(path: Path) -> set[str]:
    """Read active owner/name entries, ignoring blank and comment lines."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise PRWorkflowError(f"Could not read allow-list {path}: {exc}") from exc
    return {
        line.strip().casefold() for line in lines
        if line.strip() and not line.lstrip().startswith("#")
    }


def _run(command: Sequence[str], timeout: int = 120) -> tuple[int, str, str]:
    """Run a bounded local command without shell interpolation."""
    try:
        result = subprocess.run(
            list(command), capture_output=True, text=True, timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PRWorkflowError(f"Command failed to start: {type(exc).__name__}") from exc
    return result.returncode, result.stdout, result.stderr


def _git(
    clone: Path, args: Sequence[str],
    runner: Callable[[Sequence[str], int], tuple[int, str, str]],
) -> tuple[int, str, str]:
    """Run Git with an explicit -C target inside the fresh clone."""
    return runner(["git", "-C", str(clone), *args], 120)


def _checked_git(
    clone: Path, args: Sequence[str],
    runner: Callable[[Sequence[str], int], tuple[int, str, str]],
) -> str:
    """Run Git and raise a concise error on failure."""
    code, stdout, stderr = _git(clone, args, runner)
    if code:
        raise PRWorkflowError(
            f"git {' '.join(args[:2])} failed: {(stderr or stdout)[-1000:]}"
        )
    return stdout


def _verify_clone(
    clone: Path, runner: Callable[[Sequence[str], int], tuple[int, str, str]],
) -> None:
    """Require the clone's Git top-level to equal its isolated run folder."""
    top = Path(_checked_git(clone, ["rev-parse", "--show-toplevel"], runner)).resolve()
    if top != clone.resolve():
        raise PRWorkflowError(f"clone top-level mismatch: {top}")


def _eligibility_reasons(report: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    """Select exactly one PR-eligible package attempt and explain failures."""
    attempts = report.get("attempts", [])
    if not isinstance(attempts, list) or not attempts:
        return None, ["pipeline report contains no package attempts"]
    eligible: list[dict[str, Any]] = []
    reasons: list[str] = []
    forbidden = {
        "overfit", "injection_followed", "shadowing_suspected",
        "tampering_suspected", "reward_hacking",
    }
    for attempt in attempts:
        if not isinstance(attempt, dict):
            continue
        local: list[str] = []
        if attempt.get("result") not in {"agent_fixed", "safe_upgrade"}:
            local.append(f"outcome is {attempt.get('result', 'unknown')}")
        if attempt.get("proved_clean") is not True:
            local.append("proved_clean is not true")
        if (
            attempt.get("correct") is not None or attempt.get("category")
        ) and attempt.get("correct") is not True:
            local.append("scored attempt is not correct")
        flags = [name for name in forbidden if attempt.get(name)]
        if flags:
            local.append("disqualifying flags: " + ", ".join(sorted(flags)))
        if attempt.get("pr_eligible") is False:
            local.append("attempt is marked not PR-eligible")
        if not local:
            eligible.append(attempt)
        else:
            reasons.extend(f"{attempt.get('name', 'package')}: {reason}" for reason in local)
    if len(eligible) == 1:
        return eligible[0], []
    if len(eligible) > 1:
        return None, ["multiple packages are eligible; create one PR per package"]
    return None, reasons or ["no package attempt is eligible"]


def _branch_name(attempt: dict[str, Any], diff: str) -> str:
    """Build the required package/version branch with a short diff hash."""
    part = lambda value: re.sub(r"[^A-Za-z0-9._-]+", "-", str(value)).strip("-.")
    digest = hashlib.sha256(diff.encode("utf-8")).hexdigest()[:8]
    return (
        f"cobbsagent/{part(attempt.get('name', 'package'))}-"
        f"{part(attempt.get('version', 'unknown'))}-to-"
        f"{part(attempt.get('final_version', 'unknown'))}-{digest}"
    )


def _patch_files(attempt: dict[str, Any], project_root: Path) -> list[Path]:
    """Validate patch artifact paths before applying them to a clone."""
    patches = attempt.get("patches", {})
    if not isinstance(patches, dict) or not patches:
        raise PRWorkflowError("eligible attempt has no saved agent/bump patch")
    result: list[Path] = []
    for kind in ("agent", "bump"):
        value = patches.get(kind)
        if not value:
            continue
        path = Path(value)
        path = path if path.is_absolute() else project_root / path
        resolved = path.resolve()
        if not resolved.is_relative_to(project_root.resolve()) or not resolved.is_file():
            raise PRWorkflowError(f"patch path is missing or outside the project: {path}")
        result.append(resolved)
    if not result:
        raise PRWorkflowError("eligible attempt has no applicable patches")
    return result


def _changed_paths(
    clone: Path, runner: Callable[[Sequence[str], int], tuple[int, str, str]],
) -> list[str]:
    """Read changed paths and reject rename records that need manual handling."""
    output = _checked_git(
        clone, ["status", "--porcelain", "--untracked-files=all"], runner,
    )
    paths: list[str] = []
    for line in output.splitlines():
        if len(line) < 4:
            continue
        status, relative = line[:2], line[3:]
        if "R" in status or "C" in status or " -> " in relative:
            raise PRWorkflowError("renamed/copied paths require manual review")
        if relative.startswith('"') and relative.endswith('"'):
            relative = json.loads(relative)
        normalized = relative.replace("\\", "/")
        path = Path(*normalized.split("/"))
        if (
            path.is_absolute() or PureWindowsPath(relative).is_absolute()
            or ".." in path.parts or not normalized
        ):
            raise PRWorkflowError(f"unsafe changed path: {relative}")
        resolved = (clone / path).resolve()
        if not resolved.is_relative_to(clone.resolve()):
            raise PRWorkflowError(f"changed path escapes clone: {relative}")
        paths.append(normalized)
    return sorted(set(paths))


def _check_changed_files(clone: Path, paths: list[str]) -> None:
    """Reject protected paths and secret-like content before any commit."""
    for relative in paths:
        parts = [part.casefold() for part in relative.split("/")]
        name = parts[-1]
        if (
            "tests" in parts or any(part.startswith("tests_") for part in parts)
            or name in {"test.py", "conftest.py"} or name.startswith(("test_", "test-"))
            or name.endswith("_test.py") or name == ".env"
            or name.startswith(".env.")
            or "traces" in parts or "reports" in parts
            or name.startswith(("report", "results"))
        ):
            raise PRWorkflowError(f"protected path changed: {relative}")
        file_path = (clone / Path(*relative.split("/"))).resolve()
        try:
            content = file_path.read_bytes()
        except OSError as exc:
            raise PRWorkflowError(f"Could not scan changed file {relative}: {exc}") from exc
        if SECRET_RE.search(content.decode("utf-8", errors="replace")):
            raise PRWorkflowError(f"secret pattern found in changed file: {relative}")


def _full_diff(
    clone: Path, runner: Callable[[Sequence[str], int], tuple[int, str, str]],
) -> str:
    """Include tracked and newly created files in the review diff."""
    sections = [_checked_git(clone, ["diff", "--"], runner)]
    untracked = _checked_git(
        clone, ["ls-files", "--others", "--exclude-standard"], runner,
    )
    for relative in untracked.splitlines():
        code, stdout, stderr = _git(
            clone, ["diff", "--no-index", "--", "/dev/null", relative], runner,
        )
        if code not in {0, 1}:
            raise PRWorkflowError(f"could not diff new file {relative}: {stderr[-1000:]}")
        if stdout:
            sections.append(stdout)
    return "\n".join(section for section in sections if section)


def _build_pr_body(
    report: dict[str, Any], attempt: dict[str, Any], files: list[str], diff: str,
) -> str:
    """Render a PR body from structured fields without advisory descriptions."""
    baseline = report.get("baseline", {})
    rounds = attempt.get("rounds", [])
    round_lines = [
        f"- Round {item.get('round')}: {item.get('from')} -> {item.get('to')}; "
        f"tests_passed={item.get('tests_passed')}; agent_status={item.get('agent_status')}; "
        f"vulns_remaining={', '.join(item.get('vulns_remaining', [])) or 'none'}"
        for item in rounds if isinstance(item, dict)
    ]
    models = sorted({
        f"{item.get('backend', 'unknown')}/{item.get('model', '')}"
        for item in attempt.get("backend_models", []) if isinstance(item, dict)
    })
    final_test = rounds[-1].get("tests_passed") if rounds else None
    verification = (
        "A hidden test was run."
        if attempt.get("verified_by") == "hidden_test"
        else "Visible tests only; no hidden test was run."
    )
    return "\n".join([
        f"Advisories fixed: {', '.join(attempt.get('vuln_ids', [])) or 'none'}",
        f"Advisories remaining: {', '.join(attempt.get('vulns_remaining', [])) or 'none'}",
        f"Version: {attempt.get('version')} -> {attempt.get('final_version')}",
        f"Tests before: {baseline.get('tests_passed')}; tests after: {final_test}",
        "Rounds:", *(round_lines or ["- No repair rounds were needed."]),
        f"Steps: {attempt.get('steps_used', 0)}",
        f"Model: {', '.join(models) or 'not recorded'}",
        f"Sandbox: {attempt.get('sandbox', report.get('sandbox', 'venv'))}",
        f"Verification: {verification}",
        f"Files changed: {', '.join(files)}",
        "How to reproduce: run the case with `python cobbsagent_m2.py <case>`.",
        "Diff:", "```diff", diff, "```",
        "Generated by an automated agent. Review before merging. The agent never merges.",
    ])


def _append_log(path: Path, record: dict[str, Any]) -> None:
    """Append one token-free workflow event to pr_log.jsonl."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=True) + "\n")


def _request_pull_request(
    owner: str, repository: str, branch: str, title: str, body: str,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> str:
    """Create a GitHub PR with a header-only token and return its URL."""
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise PRWorkflowError("GITHUB_TOKEN is required for --apply")
    url = f"https://api.github.com/repos/{owner}/{repository}/pulls"
    payload = json.dumps({
        "title": title, "head": branch, "base": "main", "body": body,
    }).encode("utf-8")
    request = urllib.request.Request(
        url, data=payload, method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with opener(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise PRWorkflowError(f"GitHub PR creation failed with HTTP {exc.code}") from None
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise PRWorkflowError(f"GitHub PR creation failed: {type(exc).__name__}") from None
    link = result.get("html_url") if isinstance(result, dict) else None
    if not isinstance(link, str):
        raise PRWorkflowError("GitHub response did not contain a PR URL")
    return link


def run_pr(
    repo_url: str, case: Path, *, apply: bool = False, yes: bool = False,
    backend: Any = None,
    pipeline_fn: Callable[[Path, Any], dict[str, Any]] | None = None,
    project_root: Path = PROJECT_ROOT,
    git_runner: Callable[[Sequence[str], int], tuple[int, str, str]] = _run,
    http_opener: Callable[..., Any] = urllib.request.urlopen,
    stdin_is_terminal: bool | None = None,
    confirm: Callable[[str], str] = input,
) -> dict[str, Any]:
    """Run the pipeline, validate eligibility, then dry-run or apply one PR."""
    timestamp = datetime.now(timezone.utc).isoformat()
    record: dict[str, Any] = {
        "timestamp": timestamp, "case": str(case), "dry_run": not apply,
        "branch": None, "pr_url": None, "outcome": "error",
    }
    try:
        owner, repository, canonical_url = parse_repo_url(repo_url)
        allowed = _allowed_repositories(project_root / "allowed_repos.txt")
        if f"{owner}/{repository}".casefold() not in allowed:
            raise PRWorkflowError("repository is not on the active allow-list")
        case_path = case.resolve()
        if not case_path.is_relative_to(project_root.resolve()):
            raise PRWorkflowError("case must be inside the project folder")
        if not case_path.is_dir():
            raise PRWorkflowError(f"case is not a directory: {case}")
        if pipeline_fn is None:
            from cobbsagent_m2 import run_repository

            pipeline_fn = lambda repo, selected_backend: run_repository(
                repo, selected_backend, runs=1,
            )
        report = pipeline_fn(case_path, backend)
        if not isinstance(report, dict):
            raise PRWorkflowError("pipeline returned an invalid report")
        attempt, reasons = _eligibility_reasons(report)
        if attempt is None:
            record["outcome"] = "ineligible"
            print("PR not eligible: " + "; ".join(reasons))
            return record

        patches = _patch_files(attempt, project_root.resolve())
        run_folder = project_root / "pr_workdir" / (
            "run-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        )
        run_folder.parent.mkdir(parents=True, exist_ok=True)
        run_folder.mkdir()
        code, out, err = git_runner(
            ["git", "-C", str(run_folder), "clone", "--depth", "1", canonical_url, "."],
            300,
        )
        if code:
            raise PRWorkflowError(f"shallow clone failed: {(err or out)[-1000:]}")
        _verify_clone(run_folder, git_runner)
        for patch in patches:
            _checked_git(run_folder, ["apply", "--check", str(patch)], git_runner)
            _checked_git(run_folder, ["apply", str(patch)], git_runner)
        paths = _changed_paths(run_folder, git_runner)
        if not paths:
            raise PRWorkflowError("patches produced no changed files")
        _check_changed_files(run_folder, paths)
        diff = _full_diff(run_folder, git_runner)
        if SECRET_RE.search(diff):
            raise PRWorkflowError("secret pattern found in generated diff")
        branch = _branch_name(attempt, diff)
        remote_code, remote_out, remote_err = _git(
            run_folder, ["ls-remote", "--heads", "origin", f"refs/heads/{branch}"],
            git_runner,
        )
        if remote_code:
            raise PRWorkflowError(f"could not check remote branch: {remote_err[-1000:]}")
        if remote_out.strip():
            raise PRWorkflowError(f"remote branch already exists: {branch}")
        title = (
            f"Security: bump {attempt['name']} {attempt['version']} "
            f"-> {attempt['final_version']}"
        )
        body = _build_pr_body(report, attempt, paths, diff)
        record["branch"] = branch
        print(f"Title: {title}")
        if not apply:
            _checked_git(run_folder, ["switch", "-c", branch], git_runner)
            print(body)
            record["outcome"] = "dry_run"
            return record

        print(f"Diff summary: {len(paths)} changed file(s), {len(diff.splitlines())} lines")
        if stdin_is_terminal is None:
            stdin_is_terminal = sys.stdin.isatty()
        accepted = (
            confirm("Type APPLY to continue: ") == "APPLY"
            if stdin_is_terminal else yes
        )
        if not accepted:
            record["outcome"] = "cancelled"
            print("Apply cancelled; no commit, push, or GitHub API call was made.")
            return record
        if not os.environ.get("GITHUB_TOKEN"):
            raise PRWorkflowError("GITHUB_TOKEN is required for --apply")
        name_code, name, _ = _git(run_folder, ["config", "user.name"], git_runner)
        email_code, email, _ = _git(run_folder, ["config", "user.email"], git_runner)
        if name_code or email_code or not name.strip() or not email.strip():
            raise PRWorkflowError("set repository-local git user.name and user.email first")
        _checked_git(run_folder, ["switch", "-c", branch], git_runner)
        _checked_git(run_folder, ["add", "--", *paths], git_runner)
        _checked_git(run_folder, ["commit", "-m", title], git_runner)
        _checked_git(run_folder, ["push", "-u", "origin", branch], git_runner)
        pr_url = _request_pull_request(
            owner, repository, branch, title, body, opener=http_opener,
        )
        record.update(pr_url=pr_url, outcome="created")
        print(f"Pull request: {pr_url}")
        return record
    except PRWorkflowError as exc:
        record["outcome"] = "error"
        record["reason"] = str(exc)[:1000]
        raise
    finally:
        _append_log(project_root / "pr_log.jsonl", record)


def main() -> int:
    """Parse PR workflow options and initialize the configured repair backend."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-url", required=True)
    parser.add_argument("--case", required=True, type=Path)
    parser.add_argument("--apply", action="store_true", help="commit, push, and open a PR")
    parser.add_argument("--yes", action="store_true", help="confirm only when stdin is non-interactive")
    args = parser.parse_args()
    try:
        from agent import ResponseCache
        from cobbsagent_m2 import create_backend_chain

        specs = os.environ.get(
            "COBBSAGENT_BACKENDS",
            "gemini:" + (
                os.environ.get("COBBSAGENT_MODEL")
                or os.environ.get("FREE_MODEL")
                or "gemini-3.5-flash-lite"
            ) + ",ollama:" + os.environ.get(
                "COBBSAGENT_OLLAMA_MODEL", "qwen2.5-coder:7b",
            ),
        )
        budget = int(os.environ.get("COBBSAGENT_DAILY_BUDGET", "400"))
        backend = create_backend_chain(specs, budget, ResponseCache())
        run_pr(args.repo_url, args.case, apply=args.apply, yes=args.yes, backend=backend)
    except (ValueError, PRWorkflowError, OSError) as exc:
        print(f"PR workflow stopped: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
