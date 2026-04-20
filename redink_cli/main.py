"""`redink` CLI — user-facing entry point.

Commands:
    redink guide         cheat sheet of common commands
    redink init          interactive bootstrap (Docker, GitHub App, Slack App, engine)
    redink review URL    kick off a review; prints the session id and follows status
    redink status ID     show status of a review session
    redink answer ID     submit answers to a pending clarification round
    redink doctor        verify every component is reachable
    redink up | down     start / stop the Docker Compose stack
"""

from __future__ import annotations

import time

import httpx
import typer
from rich import print as rprint
from rich.console import Console
from rich.live import Live
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from services.config import settings

# What we say to the user for each state the server reports. Tuned to describe
# the *work* happening under the hood rather than the state-machine label.
_STATUS_LABELS: dict[str, str] = {
    "INGEST": "Registering the review session…",
    "GATHER_CONTEXT": "Reading the repo, docs, tickets, and linked issues…",
    "EVALUATE_CONTEXT": "Thinking about whether I have enough context…",
    "AWAIT_SLACK_CLARIFICATION": "Waiting for a clarification reply…",
    "REVIEW": "Reviewing the diff with the model…",
    "POST": "Posting findings to GitHub…",
    "MONITORING": "Watching the PR for replies…",
    "AWAIT_COMMENT_REPLY": "Watching the PR for replies…",
    "ENGAGE_ON_COMMENT": "Responding to a comment reply…",
    "DONE": "Done.",
    "FAILED": "Failed.",
}

_TERMINAL = {"DONE", "FAILED", "AWAIT_SLACK_CLARIFICATION", "MONITORING"}

app = typer.Typer(
    help="Redink — AI PR reviewer with Slack clarification. `redink init` to get started.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

# Redink wordmark: bold white on red background. Padded with spaces so it reads
# as a badge/pill rather than bare text. Used in the CLI header, guide, status.
WORDMARK = "[bold white on red] REDINK [/]"


def _api() -> str:
    return settings().redink_api_url.rstrip("/")


@app.command()
def review(
    url: str = typer.Argument(..., help="GitHub PR URL"),
    engine: str | None = typer.Option(
        None,
        "--engine",
        "-e",
        help="Engine to use: ollama | claude-code. Default: REDINK_ENGINE.",
    ),
    model: str | None = typer.Option(
        None,
        "--model",
        "-m",
        help=(
            "Specific model for the chosen engine "
            "(e.g. 'gemma4:e2b', 'gemma3:12b', 'claude-sonnet-4-6', "
            "'claude-opus-4-5'). Default: engine's configured model."
        ),
    ),
    mode: str = typer.Option("fresh", help="fresh | resume | restart | incremental"),
    watch: bool = typer.Option(True, "--watch/-W", help="Follow status until terminal (default on)"),
) -> None:
    """Submit a PR for review."""
    try:
        # POST returns immediately now — the server advances in the background.
        r = httpx.post(
            f"{_api()}/reviews",
            json={"pr_url": url, "engine": engine, "model": model, "mode": mode},
            timeout=30,
        )
        r.raise_for_status()
    except httpx.HTTPError as exc:
        rprint(f"[red]failed to submit review:[/red] {exc}")
        raise typer.Exit(1) from exc

    data = r.json()
    rprint(f"{WORDMARK} [green]submitted[/green] session=[bold]{data['id']}[/bold]")
    if watch:
        _watch_with_spinner(data["id"])


@app.command()
def status(session_id: str) -> None:
    """Show status of a review session."""
    r = httpx.get(f"{_api()}/reviews/{session_id}", timeout=30)
    if r.status_code == 404:
        rprint(f"[red]no such session:[/red] {session_id}")
        raise typer.Exit(1)
    r.raise_for_status()
    _render_status(r.json())


@app.command()
def answer(
    session_id: str,
    text: str = typer.Option(..., "--text", "-t", help="Free-form answer covering the open questions"),
    watch: bool = typer.Option(True, "--watch/-W", help="Follow status until terminal (default on)"),
) -> None:
    """Submit an answer to the pending clarification round and resume the review."""
    r = httpx.post(
        f"{_api()}/reviews/{session_id}/clarify",
        json={"answers": {"_free_form": text}},
        timeout=30,
    )
    if r.status_code == 404:
        rprint(f"[red]no such session:[/red] {session_id}")
        raise typer.Exit(1)
    if r.status_code == 409:
        rprint("[yellow]no pending clarification to answer[/yellow]")
        raise typer.Exit(1)
    r.raise_for_status()
    rprint(f"[green]answer accepted[/green] — resuming review for {session_id}")
    if watch:
        _watch_with_spinner(session_id)


@app.command()
def doctor() -> None:
    """Verify every component is reachable and configured."""
    from redink_cli.doctor import run_doctor

    run_doctor()


@app.command()
def init() -> None:
    """Interactive bootstrap: Docker, GitHub App, Slack App, engine choice."""
    from redink_cli.init_wizard import run_wizard

    run_wizard()


_PID_FILE = ".redink-api.pid"
_LOG_FILE = ".redink-api.log"


def _is_local_mode() -> bool:
    """Local mode = SQLite DB + api running in venv (no Docker).

    We key off DATABASE_URL (written by install-redink) rather than a separate
    flag because the DB URL is the load-bearing difference.
    """
    return settings().database_url.startswith("sqlite")


@app.command()
def up() -> None:
    """Start the stack. Auto-detects local (venv api) vs docker-compose mode."""
    import os
    import subprocess
    import time
    from pathlib import Path

    if not _is_local_mode():
        subprocess.run(["docker", "compose", "up", "-d"], check=True)
        return

    pid_file = Path(_PID_FILE)
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)  # signal 0 = probe
            rprint(f"[yellow]redink-api already running (pid {pid})[/yellow]")
            return
        except (OSError, ValueError):
            pid_file.unlink(missing_ok=True)

    log_fp = open(_LOG_FILE, "ab")
    proc = subprocess.Popen(
        ["redink-api"],
        stdout=log_fp,
        stderr=subprocess.STDOUT,
        start_new_session=True,  # detach so Ctrl+C on the CLI doesn't kill the api
    )
    pid_file.write_text(str(proc.pid))
    # Wait briefly for /healthz so the user knows it came up.
    for _ in range(20):
        try:
            r = httpx.get(f"{_api()}/healthz", timeout=1)
            if r.status_code == 200:
                rprint(f"{WORDMARK} [green]api started[/green] pid={proc.pid} logs={_LOG_FILE}")
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    rprint(
        f"[yellow]redink-api started (pid {proc.pid}) but /healthz didn't respond in 10s.[/yellow]\n"
        f"Check [cyan]{_LOG_FILE}[/cyan]."
    )


@app.command()
def down() -> None:
    """Stop the stack. Auto-detects local (kill pid) vs docker-compose mode."""
    import os
    import signal
    import subprocess
    from pathlib import Path

    if not _is_local_mode():
        subprocess.run(["docker", "compose", "down"], check=True)
        return

    pid_file = Path(_PID_FILE)
    if not pid_file.exists():
        rprint("[yellow]no redink-api pid file — nothing to stop[/yellow]")
        return
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        rprint(f"[green]stopped redink-api[/green] (pid {pid})")
    except (OSError, ValueError) as exc:
        rprint(f"[yellow]couldn't stop redink-api:[/yellow] {exc}")
    finally:
        pid_file.unlink(missing_ok=True)


@app.command()
def uninstall(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation and actually delete."),
    db: bool = typer.Option(False, "--db", help="Also delete the local SQLite DB file (if DATABASE_URL is sqlite)."),
    env: bool = typer.Option(False, "--env", help="Also delete the .env file. WARNING: contains your tokens."),
    venv: bool = typer.Option(False, "--venv", help="Also delete the local .venv/ directory."),
    docker_volumes: bool = typer.Option(
        False, "--docker-volumes", help="In Docker mode, also run `docker compose down -v` (wipes DB volume)."
    ),
) -> None:
    """Remove Redink's runtime artefacts from *this* directory.

    Safe by design:
    • Only touches files under the current working directory — paths are resolved
      and rejected if they escape cwd via symlinks or `..`.
    • Dry-run by default; requires `--yes` to actually delete.
    • Destructive targets (DB, .env, venv, docker volumes) are opt-in flags.
    • Never deletes source code, never uninstalls the Python package itself
      (use `pipx uninstall redink` / `uv tool uninstall redink` for that).

    Always deletes (if present): .redink-api.pid, .redink-api.log.
    """
    import shutil
    import subprocess
    from pathlib import Path
    from urllib.parse import urlparse

    cwd = Path.cwd().resolve()

    def _safe(p: Path) -> Path | None:
        """Resolve and ensure the path is inside cwd. Return None if it escapes or doesn't exist."""
        try:
            resolved = p.resolve(strict=False)
        except OSError:
            return None
        if not resolved.exists():
            return None
        try:
            resolved.relative_to(cwd)
        except ValueError:
            rprint(f"[yellow]skip (outside cwd):[/yellow] {resolved}")
            return None
        return resolved

    # --- Build the plan -------------------------------------------------
    targets: list[tuple[str, Path, str]] = []  # (label, resolved_path, kind: "file"|"dir")

    # Always: pid + log
    for name in (_PID_FILE, _LOG_FILE):
        p = _safe(Path(name))
        if p is not None:
            targets.append(("runtime file", p, "file"))

    # --db: local SQLite DB
    if db:
        dsn = settings().database_url
        if dsn.startswith("sqlite"):
            # sqlite:///relative/path.db  or  sqlite:////absolute/path.db
            # urlparse gives us the path after the scheme.
            parsed = urlparse(dsn)
            # `sqlite:///foo.db` -> parsed.path = "/foo.db" (leading slash is separator, not abs)
            # `sqlite:////abs/foo.db` -> parsed.path = "//abs/foo.db"
            raw = parsed.path
            db_path = Path(raw[1:]) if raw.startswith("/") and not raw.startswith("//") else Path(raw)
            if not db_path.is_absolute():
                db_path = cwd / db_path
            p = _safe(db_path)
            if p is not None:
                targets.append(("sqlite database", p, "file"))
                # SQLite sidecar files created by WAL mode
                for sidecar in (p.with_name(p.name + "-wal"), p.with_name(p.name + "-shm")):
                    sp = _safe(sidecar)
                    if sp is not None:
                        targets.append(("sqlite sidecar", sp, "file"))
        else:
            rprint("[yellow]--db ignored:[/yellow] DATABASE_URL is not sqlite; drop it manually if you want.")

    # --env
    if env:
        p = _safe(Path(".env"))
        if p is not None:
            targets.append(("env file (contains secrets)", p, "file"))

    # --venv
    if venv:
        p = _safe(Path(".venv"))
        if p is not None and p.is_dir():
            # Extra sanity: must contain pyvenv.cfg. Prevents `--venv` from nuking a
            # random dir someone happened to name `.venv/`.
            if (p / "pyvenv.cfg").exists():
                targets.append(("python venv", p, "dir"))
            else:
                rprint(f"[yellow]skip (no pyvenv.cfg):[/yellow] {p}")

    # --- Print the plan -------------------------------------------------
    if not targets and not docker_volumes:
        rprint("[green]nothing to remove.[/green] No Redink artefacts found in this directory.")
        return

    rprint("[bold]Redink uninstall plan[/bold]")
    rprint(f"  working dir: [cyan]{cwd}[/cyan]")
    if targets:
        rprint("  will delete:")
        for label, path, kind in targets:
            marker = ":file_folder:" if kind == "dir" else ":page_facing_up:"
            rprint(f"    {marker} [red]{path}[/red]  [dim]({label})[/dim]")
    if docker_volumes:
        rprint("  will run: [red]docker compose down -v[/red]  [dim](wipes named volumes)[/dim]")
    rprint(
        "\n[bold]Will NOT touch:[/bold] source code, installed package, "
        "global venvs, anything outside this directory."
    )

    if not yes:
        rprint("\n[yellow]Dry run.[/yellow] Re-run with [cyan]--yes[/cyan] to actually delete.")
        return

    # --- Stop the API first so it doesn't recreate files we're about to delete.
    rprint("\n[bold]Stopping redink-api...[/bold]")
    try:
        down()
    except typer.Exit:
        pass  # down() exits on missing pid file; that's fine, we carry on.
    except Exception as exc:  # noqa: BLE001
        rprint(f"[yellow]ignore stop error:[/yellow] {exc}")

    # --- Execute ---------------------------------------------------------
    errors = 0
    for label, path, kind in targets:
        # Re-check it's still under cwd right before deleting (TOCTOU guard).
        try:
            path.relative_to(cwd)
        except ValueError:
            rprint(f"[yellow]skip (escaped cwd):[/yellow] {path}")
            continue
        try:
            if kind == "dir":
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
            rprint(f"  [green]removed[/green] {path} [dim]({label})[/dim]")
        except OSError as exc:
            rprint(f"  [red]failed:[/red] {path} — {exc}")
            errors += 1

    if docker_volumes:
        try:
            subprocess.run(["docker", "compose", "down", "-v"], check=True)
            rprint("  [green]docker compose down -v[/green] complete")
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            rprint(f"  [red]docker compose down -v failed:[/red] {exc}")
            errors += 1

    if errors:
        rprint(f"\n[yellow]finished with {errors} error(s).[/yellow]")
        raise typer.Exit(1)
    rprint("\n[green]done.[/green] To uninstall the CLI itself: [cyan]pipx uninstall redink[/cyan] (or `uv tool uninstall redink`).")


@app.command()
def help() -> None:  # noqa: A001 — the intent of the command IS "help"
    """Open the full docs (docs/cli.md + docs/slack.md)."""
    import os
    import subprocess
    from pathlib import Path

    # Locate docs relative to this file; works whether installed in-place or as a package.
    # We walk up looking for a `docs/` dir so editable installs + site-packages both work.
    here = Path(__file__).resolve()
    docs_dir: Path | None = None
    for parent in (here.parent, *here.parents):
        candidate = parent / "docs"
        if (candidate / "cli.md").exists():
            docs_dir = candidate
            break

    if docs_dir is None:
        rprint(
            "[yellow]couldn't locate docs/ next to the install.[/yellow] "
            "See the docs online: https://github.com/<org>/redink/tree/main/docs"
        )
        return

    cli_md = docs_dir / "cli.md"
    slack_md = docs_dir / "slack.md"
    rprint(f"[bold]CLI reference:[/bold]   {cli_md}")
    rprint(f"[bold]Slack reference:[/bold] {slack_md}")

    # If we're in a TTY try to open the CLI doc in the user's pager. Otherwise just print the path.
    if os.isatty(1):
        pager = os.environ.get("PAGER", "less -R")
        try:
            subprocess.run(f"{pager} {cli_md}", shell=True, check=False)
            return
        except OSError:
            pass
    # Fallback: dump the file.
    rprint("\n" + cli_md.read_text())


@app.command()
def guide() -> None:
    """Print a cheat sheet of common Redink commands."""
    console.print(f"\n{WORDMARK}  [bold]cheat sheet[/bold]")
    console.print(
        """

[bold cyan]First-time setup[/bold cyan]
  ./install-redink                    one-shot: venv + install + docker + .env + ollama
  source .venv/bin/activate           activate venv in a new shell
  redink doctor                       verify every component is green

[bold cyan]Reviewing a PR[/bold cyan]
  redink review <url>                                       submit a review (returns a session id)
  redink review <url> --watch                               submit + follow status until terminal
  redink review <url> --engine ollama                       override REDINK_ENGINE for one run
  redink review <url> -e ollama      -m gemma4:e2b          small local model (default)
  redink review <url> -e ollama      -m gemma3:12b          bigger local model
  redink review <url> -e claude-code -m claude-sonnet-4-6   Claude Sonnet 4.6 via Claude Code CLI
  redink review <url> -e claude-code -m claude-opus-4-5     Opus 4.5 via Claude Code CLI
  redink review <url> --mode fresh|resume|restart|incremental

[bold cyan]During / after a review[/bold cyan]
  redink status <id>                  show current state and pending questions
  redink answer <id> --text "..."     answer a pending clarification round
                                      (equivalent to replying in the Slack thread)

[bold cyan]Stack control[/bold cyan]
  redink up                           docker compose up -d
  redink down                         docker compose down
  docker compose logs -f api          tail API logs while a review runs
  docker compose exec ollama ollama list   see pulled models

[bold cyan]Typical end-to-end run[/bold cyan]
  ./install-redink
  redink review https://github.com/<org>/<repo>/pull/<n> --watch
  # → comments appear on the PR; if context is ambiguous, answer with:
  redink answer <session-id> --text "PR adds optional field X for back-compat"
"""
    )


# ------------------------------------------------------------------ helpers


def _render_status(data: dict) -> None:
    console.print(f"\n{WORDMARK}  [dim]review[/dim] [bold]{data.get('id','?')}[/bold]")
    t = Table(show_header=False, box=None)
    for k in ("pr_url", "status", "engine", "model", "head_sha", "finding_count", "error"):
        v = data.get(k)
        if v is None:
            continue
        t.add_row(f"[bold]{k}[/bold]", str(v))
    console.print(t)

    pending = data.get("pending_questions") or []
    if pending:
        rprint("\n[yellow]Pending clarification:[/yellow]")
        for q in pending:
            rprint(f"  [bold]{q['id']}.[/bold] {q['text']}")
            rprint(f"     [dim]why: {q['why_needed']}[/dim]")
        rprint(
            f"\nReply with: [cyan]redink answer {data['id']} "
            "--text \"...\"[/cyan] or in the Slack thread."
        )


def _watch_with_spinner(session_id: str) -> None:
    """Poll the session and display a Rich spinner labelled with the current phase.

    Keeps the CLI responsive during long LLM calls — users see "Reviewing the
    diff with the model…" rather than a frozen prompt. When the session reaches
    a terminal state (DONE, FAILED, AWAIT_SLACK_CLARIFICATION) we stop the
    spinner and print a final status table.
    """
    last_status: str | None = None
    data: dict = {}
    spinner = Spinner("dots", text="Starting…", style="cyan")
    try:
        with Live(spinner, console=console, refresh_per_second=10, transient=True) as live:
            while True:
                try:
                    r = httpx.get(f"{_api()}/reviews/{session_id}", timeout=10)
                    r.raise_for_status()
                    data = r.json()
                except httpx.HTTPError as exc:
                    spinner.update(text=Text(f"api unreachable ({exc}) — retrying…", style="yellow"))
                    live.update(spinner)
                    time.sleep(1.0)
                    continue

                status = data.get("status") or "?"
                label = _STATUS_LABELS.get(status, status)
                if status != last_status:
                    spinner.update(
                        text=Text.assemble((f"{label}  ", "cyan"), (f"({status})", "dim")),
                    )
                    live.update(spinner)
                    last_status = status
                if status in _TERMINAL:
                    break
                time.sleep(0.4)
    finally:
        _render_status(data or {"id": session_id, "status": "UNKNOWN"})


if __name__ == "__main__":
    app()
