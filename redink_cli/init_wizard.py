"""`redink init` — interactive bootstrap.

M1 scope: just enough to get `redink review <url>` working. Walks the user through:
  1. Check Docker is installed and running.
  2. Pick an engine (ollama / claude-code / anthropic / openai).
  3. Write ~/.redink/.env with selected engine + placeholders for GitHub/Slack.
  4. Copy .env into the repo root for docker-compose.
  5. `docker compose up -d` the stack.
  6. If ollama: pull the selected model.
  7. Print next steps (GitHub App manifest flow, Slack App manifest flow) — M2+
     will automate those.

Full auto GitHub/Slack manifest flow lands in M2; v0 asks the user to paste the
resulting secrets and stores them.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import httpx
import typer
from rich import print as rprint
from rich.prompt import Confirm, Prompt

ENGINES = ["ollama", "claude-code", "anthropic", "openai"]
DEFAULT_OLLAMA_MODEL = "gemma4:e2b"


def run_wizard() -> None:
    rprint("[bold]Redink setup[/bold] — this takes ~2 minutes.\n")

    _require_docker()
    engine = _pick_engine()
    ollama_model = DEFAULT_OLLAMA_MODEL
    if engine == "ollama":
        ollama_model = Prompt.ask(
            "Ollama model", default=DEFAULT_OLLAMA_MODEL, show_default=True
        )

    repo_root = _repo_root()
    env_path = repo_root / ".env"
    _write_env(env_path, engine=engine, ollama_model=ollama_model)
    rprint(f"  wrote [cyan]{env_path}[/cyan]")

    if Confirm.ask("Start the stack with `docker compose up -d` now?", default=True):
        subprocess.run(["docker", "compose", "up", "-d"], cwd=repo_root, check=True)

    if engine == "ollama":
        _pull_ollama_model(ollama_model)

    rprint("\n[bold green]Done.[/bold green] Next steps:")
    rprint(
        "  1. Create a GitHub App (webhook URL = http://<this-host>:8080/webhooks/github)\n"
        "     and paste its App ID + webhook secret + private-key path into .env.\n"
        "  2. Create a Slack App (manifest-based) and paste bot token + signing secret into .env.\n"
        "  3. `redink doctor` to verify everything.\n"
        "  4. `redink review <pr-url>` to kick off your first review.\n"
    )


# ---------------------------------------------------------------- internals


def _require_docker() -> None:
    if shutil.which("docker") is None:
        rprint("[red]Docker not found.[/red] Install Docker Desktop and re-run `redink init`.")
        raise typer.Exit(1)
    try:
        subprocess.run(
            ["docker", "info"], check=True, capture_output=True, timeout=5
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        rprint("[red]Docker daemon isn't running.[/red] Start Docker Desktop and re-run.")
        raise typer.Exit(1) from None


def _pick_engine() -> str:
    rprint("Pick a review engine:")
    for i, e in enumerate(ENGINES, 1):
        mark = " [dim](local, default)[/dim]" if e == "ollama" else ""
        rprint(f"  {i}. {e}{mark}")
    choice = Prompt.ask("Choice", default="1")
    try:
        return ENGINES[int(choice) - 1]
    except (ValueError, IndexError):
        rprint("[red]Invalid choice[/red]")
        raise typer.Exit(1) from None


def _repo_root() -> Path:
    # When installed via pipx this runs from site-packages; fall back to $PWD.
    here = Path(__file__).resolve().parent.parent
    if (here / "docker-compose.yml").exists():
        return here
    return Path.cwd()


def _write_env(path: Path, *, engine: str, ollama_model: str) -> None:
    example = path.with_name(".env.example")
    if path.exists():
        return  # don't clobber an existing .env; user can edit manually
    if not example.exists():
        raise RuntimeError(f"missing {example} — reinstall redink")
    content = example.read_text()
    content = content.replace("REDINK_ENGINE=ollama", f"REDINK_ENGINE={engine}")
    content = content.replace(
        "OLLAMA_MODEL=gemma4:e2b", f"OLLAMA_MODEL={ollama_model}"
    )
    path.write_text(content)
    os.chmod(path, 0o600)


def _pull_ollama_model(model: str) -> None:
    rprint(f"\nPulling Ollama model [cyan]{model}[/cyan] (this can take a few minutes)...")
    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
    try:
        with httpx.stream("POST", f"{host}/api/pull", json={"name": model}, timeout=None) as r:
            for line in r.iter_lines():
                if line:
                    rprint(f"  [dim]{line}[/dim]")
    except httpx.HTTPError as exc:
        rprint(f"[yellow]Couldn't reach Ollama at {host}:[/yellow] {exc}")
        rprint("  Is the compose stack up? Try `redink up` and re-run `redink init`.")
