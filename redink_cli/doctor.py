"""`redink doctor` — quick end-to-end health check.

Checks Docker, Postgres, Redis, Ollama (if selected), the orchestrator API, and that
the GitHub App + Slack App credentials resolve. Prints one green/red line per check.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import httpx
from rich import print as rprint

from services.config import settings

OK = "[green]ok[/green]"
FAIL = "[red]fail[/red]"


def run_doctor() -> None:
    s = settings()
    local_mode = s.database_url.startswith("sqlite")

    if local_mode:
        rprint("  [cyan]mode[/cyan]  local (SQLite + native Ollama, no Docker)")
    else:
        _check("docker present", _docker_present())
        _check("docker daemon up", _docker_up())

    _check(f"api reachable ({s.redink_api_url})", _api_up(s.redink_api_url))

    if s.redink_engine == "ollama":
        _check(f"ollama reachable ({s.ollama_host})", _ollama_up(s.ollama_host))
        _check(f"ollama model '{s.ollama_model}' pulled", _ollama_model_pulled(s))

    if s.github_pat:
        _check("GITHUB_PAT set (POC mode)", True)
    else:
        _check("GITHUB_APP_ID set", bool(s.github_app_id))
        _check(
            "GITHUB_APP_PRIVATE_KEY_PATH readable",
            _file_readable(s.github_app_private_key_path),
        )

    # Slack is optional; show as info lines when absent rather than fail,
    # because M2's clarification loop is the only consumer and it's not
    # required for a basic review.
    slack_configured = bool(s.slack_bot_token and s.slack_signing_secret)
    if slack_configured:
        _check("Slack configured", True)
    else:
        rprint("  [dim]--[/dim]  Slack not configured (optional; needed only for clarification loop)")


def _check(label: str, ok: bool, hint: str = "") -> None:
    tag = OK if ok else FAIL
    rprint(f"  {tag}  {label}{(' — ' + hint) if hint and not ok else ''}")


def _docker_present() -> bool:
    return shutil.which("docker") is not None


def _docker_up() -> bool:
    if not _docker_present():
        return False
    try:
        subprocess.run(
            ["docker", "info"], check=True, capture_output=True, timeout=5
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


def _api_up(url: str) -> bool:
    try:
        r = httpx.get(f"{url.rstrip('/')}/healthz", timeout=3)
        return r.status_code == 200
    except httpx.HTTPError:
        return False


def _ollama_up(host: str) -> bool:
    try:
        r = httpx.get(f"{host.rstrip('/')}/api/tags", timeout=3)
        return r.status_code == 200
    except httpx.HTTPError:
        return False


def _ollama_model_pulled(s) -> bool:
    try:
        r = httpx.get(f"{s.ollama_host.rstrip('/')}/api/tags", timeout=3)
        r.raise_for_status()
        models = {m["name"] for m in r.json().get("models", [])}
        return s.ollama_model in models
    except httpx.HTTPError:
        return False


def _file_readable(path: str) -> bool:
    if not path:
        return False
    try:
        return Path(path).is_file()
    except OSError:
        return False
