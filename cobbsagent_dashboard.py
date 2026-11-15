"""Compatibility entry point for the offline report dashboard."""

from cobbs.dashboard import build_dashboard, main, render_dashboard

__all__ = ["build_dashboard", "main", "render_dashboard"]

if __name__ == "__main__":
    raise SystemExit(main())
