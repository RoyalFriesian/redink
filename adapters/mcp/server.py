"""MCP stdio server — M1 skeleton.

Exposes the high-level tools (review_pr, get_status, respond_to_clarification, cancel).
The toolbelt tier (for Claude-Code-as-engine) lands in M5.
"""

from __future__ import annotations

import logging

import httpx

from services.config import settings

log = logging.getLogger(__name__)


def run() -> None:
    """Entry point for the `redink-mcp` console script."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise RuntimeError(
            "mcp SDK not installed. `pip install mcp` or reinstall redink."
        ) from exc

    logging.basicConfig(level=settings().redink_log_level)
    server = FastMCP("redink")
    api = settings().redink_api_url.rstrip("/")

    @server.tool()
    def review_pr(url: str, engine: str | None = None, mode: str = "fresh") -> dict:
        """Start a code review for a GitHub PR URL. Returns {id, status}."""
        r = httpx.post(
            f"{api}/reviews",
            json={"pr_url": url, "engine": engine, "mode": mode},
            timeout=300,
        )
        r.raise_for_status()
        return r.json()

    @server.tool()
    def get_status(session_id: str) -> dict:
        """Fetch current status of a review session."""
        r = httpx.get(f"{api}/reviews/{session_id}", timeout=30)
        r.raise_for_status()
        return r.json()

    @server.tool()
    def respond_to_clarification(session_id: str, text: str) -> dict:
        """Submit a free-form answer to a pending clarification round (M2+)."""
        r = httpx.post(
            f"{api}/reviews/{session_id}/clarify",
            json={"answers": {"_free_form": text}},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    server.run()


if __name__ == "__main__":
    run()
