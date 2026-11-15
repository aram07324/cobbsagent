"""Copy only reviewed allow-listed project files into a publish staging folder."""

from __future__ import annotations

import argparse
import re
from pathlib import Path, PureWindowsPath

PROJECT_ROOT = Path(__file__).resolve().parent
SECRET_PATTERNS = (
    ("Google API key", re.compile(r"AIza[A-Za-z0-9_-]{20,}")),
    ("GitHub token", re.compile(r"(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{20,}")),
    ("private key", re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
    )),
)
FORBIDDEN_PARTS = {
    ".git", ".env", "cache", "eval", "eval_hidden", "patches",
    "pr_workdir", "traces", "tests_agent",
}
PUBLISH_EVALUATION_FILES = {
    # These are the only evaluation files explicitly approved for publishing.
    "eval/expected.json",
    "eval_hidden/jwt_weak_tests/test_hidden.py",
}


class PublishSafetyError(RuntimeError):
    """Raised when an allow-list or content scan fails closed."""


def _relative_path(raw: str) -> Path:
    normalized = raw.strip().replace("\\", "/")
    candidate = Path(*normalized.split("/"))
    if (
        not normalized or candidate.is_absolute()
        or PureWindowsPath(raw).is_absolute()
        or ".." in candidate.parts
    ):
        raise PublishSafetyError(f"unsafe allow-list path: {raw}")
    parts = [part.casefold() for part in candidate.parts]
    name = parts[-1]
    is_explicit_evaluation_file = candidate.as_posix().casefold() in (
        PUBLISH_EVALUATION_FILES
    )
    if (
        any(
            part in FORBIDDEN_PARTS or part.startswith("eval_")
            for part in parts
        ) and not is_explicit_evaluation_file
        or name == "allowed_repos.txt" or name == ".gitignore"
        or (name == "license" and len(parts) != 1)
        or name.startswith((".env", "report", "results"))
        or name.endswith((".key", ".pem", ".p12", ".pfx"))
    ):
        raise PublishSafetyError(f"protected path is not publishable: {raw}")
    return candidate


def _scan_file(relative: Path, content: bytes) -> list[str]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PublishSafetyError(
            f"publish file is not UTF-8 text: {relative.as_posix()}"
        ) from exc
    return [
        label for label, pattern in SECRET_PATTERNS
        if pattern.search(text)
    ]


def build_publish(
    root: Path = PROJECT_ROOT, output: Path = Path("publish"),
    allowlist: Path = Path("publish_allowlist.txt"),
) -> list[str]:
    """Validate all sources before writing an exact, allow-listed file set."""
    project = root.resolve()
    list_path = allowlist if allowlist.is_absolute() else project / allowlist
    try:
        entries = [
            line.strip() for line in list_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
    except OSError as exc:
        raise PublishSafetyError(f"could not read publish allow-list: {exc}") from exc
    if not entries:
        raise PublishSafetyError("publish allow-list contains no active files")

    sources: dict[str, bytes] = {}
    findings: list[tuple[str, str]] = []
    for raw in entries:
        relative = _relative_path(raw)
        normalized = relative.as_posix()
        if normalized in sources:
            raise PublishSafetyError(f"duplicate allow-list path: {normalized}")
        source = project / relative
        resolved = source.resolve()
        cursor = project
        has_symlink_component = False
        for part in relative.parts:
            cursor = cursor / part
            has_symlink_component = has_symlink_component or cursor.is_symlink()
        if (
            not resolved.is_relative_to(project)
            or has_symlink_component or not resolved.is_file()
        ):
            raise PublishSafetyError(f"missing or unsafe source file: {normalized}")
        content = resolved.read_bytes()
        sources[normalized] = content
        findings.extend(
            (normalized, label) for label in _scan_file(relative, content)
        )
    if findings:
        summary = "; ".join(
            f"{path} ({label})" for path, label in findings
        )
        raise PublishSafetyError(f"secret scan blocked publish: {summary}")

    destination = output if output.is_absolute() else project / output
    destination = destination.resolve()
    if (
        destination == project or not destination.is_relative_to(project)
        or destination.exists()
    ):
        raise PublishSafetyError(
            "publish destination must be a new directory inside the project"
        )
    destination_parts = [
        part.casefold() for part in destination.relative_to(project).parts
    ]
    if any(part in FORBIDDEN_PARTS for part in destination_parts):
        raise PublishSafetyError("publish destination is inside a protected folder")

    for normalized, content in sources.items():
        target = destination / Path(*normalized.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return sorted(sources)


def main() -> int:
    """Run the explicit allow-list copy and report filenames only."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("publish"))
    parser.add_argument(
        "--allowlist", type=Path, default=Path("publish_allowlist.txt"),
    )
    args = parser.parse_args()
    try:
        files = build_publish(output=args.output, allowlist=args.allowlist)
    except (OSError, PublishSafetyError) as exc:
        parser.error(str(exc))
    print(f"Publish staging folder: {(PROJECT_ROOT / args.output).resolve()}")
    print("Copied files:")
    for path in files:
        print(f"  {path}")
    print("No .gitignore is generated; LICENSE is copied only when allow-listed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
