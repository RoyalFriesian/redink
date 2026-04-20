# Redink CLI Reference

Every CLI command, every flag, every example. If you're looking at `--help` output and wondering what a flag does, check here.

```text
redink <command> [options]
```

Run `redink --help` or `redink <command> --help` at any time.

---

## Quick reference

| Command | What it does |
|---|---|
| [`redink init`](#redink-init) | One-time interactive setup (Docker, GitHub, Slack, engine) |
| [`redink doctor`](#redink-doctor) | Verify every component is reachable and configured |
| [`redink up` / `redink down`](#redink-up--redink-down) | Start / stop the stack |
| [`redink review <url>`](#redink-review-url) | Submit a PR for review |
| [`redink status <id>`](#redink-status-id) | Show status of a review |
| [`redink answer <id> --text "…"`](#redink-answer-id---text) | Reply to a pending clarification round |
| [`redink guide`](#redink-guide) | In-terminal cheat sheet |
| [`redink help`](#redink-help) | Open this document |
| [`redink uninstall`](#redink-uninstall) | Remove Redink's runtime artefacts from this directory |

---

## `redink review <url>`

Submit a GitHub PR for review.

```bash
redink review https://github.com/<org>/<repo>/pull/<n> [options]
```

### Options

| Flag | Default | Description |
|---|---|---|
| `-e`, `--engine` | `REDINK_ENGINE` (`.env`) | Engine to use: `ollama` or `claude-code`. |
| `-m`, `--model` | Engine's default | Specific model. Passed to the engine; must be installed/accessible. |
| `--mode` | `fresh` | `fresh` \| `resume` \| `restart` \| `incremental`. |
| `--watch / --no-watch` | `--watch` | Spinner follows status until terminal; turn off to fire-and-forget. |

### Engine + model cheat sheet

| Engine | Model | Where it runs |
|---|---|---|
| `ollama` | `gemma4:e2b` (default) | Local Ollama — fast, free, conservative. |
| `ollama` | `gemma3:12b` | Local Ollama — slower, catches more. |
| `ollama` | `qwen2.5-coder:7b` | Local Ollama — tuned for code. |
| `claude-code` | `claude-sonnet-4-6` (default) | Claude CLI — frontier quality, ~cents/PR. |
| `claude-code` | `claude-opus-4-5` | Claude CLI — deepest review, highest cost. |

For Ollama models, pull them first: `ollama pull gemma3:12b`.
For Claude Code, the model name must be one the `claude` CLI accepts — check with `claude --help`.

### Examples

```bash
# Daily default
redink review https://github.com/org/repo/pull/42

# Force local review
redink review https://github.com/org/repo/pull/42 -e ollama -m gemma4:e2b

# Frontier review for a risky PR
redink review https://github.com/org/repo/pull/42 -e claude-code -m claude-sonnet-4-6

# Deepest review
redink review https://github.com/org/repo/pull/42 -e claude-code -m claude-opus-4-5

# Submit and exit — no spinner
redink review https://github.com/org/repo/pull/42 --no-watch

# Re-run from scratch (drops prior context + rounds)
redink review https://github.com/org/repo/pull/42 --mode restart
```

### Output

On success:
```
submitted session=89d4e645-451c-4d0b-b78b-deb25f19a5fa
⠋ Thinking about whether I have enough context…  (EVALUATE_CONTEXT)
```
When terminal, the spinner prints a final status table with `pr_url, status, engine, model, head_sha, finding_count`.

### `--mode` values

| Mode | Behaviour |
|---|---|
| `fresh` (default) | New session. Duplicate of an in-flight PR is rejected. |
| `resume` | Continue an existing session (usually picked up automatically). |
| `restart` | Wipe prior rounds + findings, start over. |
| `incremental` | Review only files changed since last review. |

---

## `redink status <id>`

Snapshot the state of a review.

```bash
redink status <session-id>           # one-shot
redink status <session-id> --watch   # stream until terminal
```

Prints a table with the effective engine + model, current phase, finding count, and — if the session is in `AWAIT_SLACK_CLARIFICATION` — the pending questions.

---

## `redink answer <id> --text "…"`

Submit an answer to the currently-open clarification round. Equivalent to replying in the Slack thread.

```bash
redink answer <session-id> --text "The new field is optional for back-compat."
```

| Flag | Default | Description |
|---|---|---|
| `-t`, `--text` | *required* | Free-form answer. One text blob, not per-question — the engine parses it. |
| `--watch / --no-watch` | `--watch` | Follow status after submitting. |

Exits `1` with `no pending clarification to answer` if the session isn't waiting.

---

## `redink doctor`

Runs a health check over every component:
- `redink-api` reachable on `REDINK_API_URL`
- Ollama host reachable (if `REDINK_ENGINE=ollama`)
- `claude` CLI installed and authenticated (if `REDINK_ENGINE=claude-code`)
- GitHub PAT / App auth works
- Slack bot token works and bot is in `SLACK_REVIEW_CHANNEL`
- Atlassian creds work (if Jira / Confluence base URLs are set)

Run it any time something goes wrong before digging into logs.

---

## `redink init`

Interactive first-time setup wizard. Walks you through:
1. Engine choice — Ollama or Claude Code
2. Ollama model pull (if Ollama selected)
3. GitHub App / PAT setup
4. Slack app setup (bot token, signing secret, app token for Socket Mode)
5. Atlassian creds (optional)

Writes `.env` and runs `redink doctor` at the end.

---

## `redink up` / `redink down`

Start / stop the stack.

- **Local mode** (SQLite `DATABASE_URL`): spawns `redink-api` in the background, writes pid to `.redink-api.pid`, logs to `.redink-api.log`.
- **Docker mode**: `docker compose up -d` / `docker compose down`.

Auto-detected from `DATABASE_URL`.

---

## `redink guide`

Pretty cheat sheet printed in the terminal. Good for "I forgot the flag for X".

---

## `redink help`

Opens the full documentation — this file plus the [Slack reference](slack.md). Run it when you need more than the cheat sheet.

---

## `redink uninstall`

Remove Redink's runtime artefacts from the **current directory**. Safe by design — it only touches files Redink itself wrote, refuses to follow paths that escape cwd, and is dry-run by default.

```bash
redink uninstall                 # dry run — shows the plan, deletes nothing
redink uninstall --yes           # actually delete (pid file + log file)
redink uninstall --db --yes      # also delete the local SQLite DB (+ WAL sidecars)
redink uninstall --env --yes     # also delete .env (WARNING: contains your tokens)
redink uninstall --venv --yes    # also delete .venv/ (only if it has pyvenv.cfg)
redink uninstall --docker-volumes --yes   # also `docker compose down -v`
```

### Options

| Flag | Default | Description |
|---|---|---|
| `-y`, `--yes` | off | Skip the dry-run preview and actually delete. |
| `--db` | off | Delete the SQLite DB file + `-wal`/`-shm` sidecars (only if `DATABASE_URL=sqlite`). |
| `--env` | off | Delete `.env`. Contains secrets — opt-in only. |
| `--venv` | off | Delete `.venv/` (must contain `pyvenv.cfg`). |
| `--docker-volumes` | off | Run `docker compose down -v` (wipes named volumes). |

### Always deleted (if present)

- `.redink-api.pid`
- `.redink-api.log`

### Never touched

- Source code, any file outside the current directory, the installed Python package itself.
- To remove the CLI binary: `pipx uninstall redink` (or `uv tool uninstall redink`).

### Safety guarantees

- Every target path is resolved (following symlinks) and rejected if it escapes the cwd.
- `--venv` refuses to delete a directory that doesn't contain `pyvenv.cfg`.
- `--db` is ignored unless `DATABASE_URL` starts with `sqlite` — Postgres you drop manually.
- Dry-run by default: you see exactly what would be deleted before committing.

### Typical clean reinstall

```bash
redink uninstall --db --venv --yes
pipx uninstall redink
# then re-clone and run ./install-redink
```

---

## Environment variables

All CLI flags can be backed by environment variables in `.env`. Full list:

| Variable | Purpose |
|---|---|
| `REDINK_ENGINE` | Default engine (`ollama` \| `claude-code`) |
| `REDINK_MODEL` | Default model override (empty = engine default) |
| `REDINK_API_URL` | Where the CLI + Slack post reviews (default `http://localhost:8080`) |
| `OLLAMA_HOST`, `OLLAMA_MODEL` | Ollama engine defaults |
| `CLAUDE_CODE_BINARY`, `CLAUDE_CODE_MODEL`, `CLAUDE_CODE_EFFORT` | Claude Code engine defaults |
| `GITHUB_PAT` | GitHub auth |
| `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, `SLACK_APP_TOKEN`, `SLACK_REVIEW_CHANNEL` | Slack |
| `ATLASSIAN_EMAIL`, `ATLASSIAN_API_TOKEN`, `JIRA_BASE_URL`, `CONFLUENCE_BASE_URL` | Jira + Confluence |
| `REDINK_MAX_CLARIFICATION_ROUNDS` | Author-facing clarification cap (default 3) |
| `REDINK_PER_PR_TOKEN_CEILING` | Hard ceiling per PR (default 200000) |

---

## Troubleshooting

| Symptom | Check |
|---|---|
| `failed to submit review: ConnectError` | `redink up` — is the API running? Is `REDINK_API_URL` correct? |
| Spinner stuck at `INGEST` | `tail -f .redink-api.log` — GitHub auth failing? |
| `ollama ... attempt N/M failed: empty response` | Model isn't pulled / host not reachable. Run `ollama list`. |
| Claude Code subprocess error | `claude --version` — is the CLI installed + logged in? |
| No Slack thread | `SLACK_BOT_TOKEN` unset, or bot not `/invite`d to channel. |
| Jira / Confluence returns 0 chunks | Check `ATLASSIAN_*` creds with `redink doctor`. |

---

See also: [Slack commands](slack.md) · [README](../README.md)
