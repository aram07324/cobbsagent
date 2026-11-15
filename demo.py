"""Run a dependency-repair demo and render its report as a dashboard."""

from __future__ import annotations

import argparse
from pathlib import Path

from cobbs.dashboard import build_dashboard

PROJECT_ROOT = Path(__file__).resolve().parent


def run_repairs(repo: Path, pipeline_args: list[str]) -> None:
    """Run milestone two with the selected repository and CLI options."""
    from cobbsagent_m2 import main as pipeline_main

    pipeline_main([str(repo), *pipeline_args])


def main(argv: list[str] | None = None) -> int:
    """Run repairs, then render the resulting dashboard."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "The live repair mode may send requests to the configured model "
            "backend. Use --dashboard-only to render saved local outputs."
        ),
    )
    parser.add_argument(
        "--repo", type=Path,
        default=PROJECT_ROOT / "eval" / "jwt_breaking",
        help="repository to repair (default: the PyJWT breaking demo case)",
    )
    parser.add_argument(
        "--dashboard-only", action="store_true",
        help="render existing outputs without running repairs",
    )
    parser.add_argument("--report", type=Path, default=Path("report.json"))
    parser.add_argument("--traces", type=Path, default=Path("traces"))
    parser.add_argument("--output", type=Path, default=Path("dashboard.html"))
    args, pipeline_args = parser.parse_known_args(argv)
    if args.dashboard_only and pipeline_args:
        parser.error("pipeline options cannot be used with --dashboard-only")
    if not args.dashboard_only and (
        args.report != Path("report.json") or args.traces != Path("traces")
    ):
        parser.error("--report and --traces are only used with --dashboard-only")

    if not args.dashboard_only:
        print(
            "Running dependency repairs. The configured model backend may "
            "make API requests; no pull request will be created."
        )
        run_repairs(args.repo, pipeline_args)

    try:
        output = build_dashboard(args.report, args.traces, args.output)
    except (OSError, ValueError) as exc:
        parser.error(f"Could not render the demo dashboard: {exc}")
    print(f"Demo dashboard ready: {output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
