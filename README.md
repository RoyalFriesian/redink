# Redink

AI PR reviewer with **Slack-mediated clarification** and **pluggable engines**.

- **Actively gathers context** before reviewing — repo docs, Jira tickets (description, comments, subtasks, parent epic), Confluence pages (explicit links + CQL search).
- **Asks when it doesn't know.** If context is insufficient, Redink opens a Slack thread on the PR and asks the author clarifying questions, then resumes the review when they reply.
- **Engages on comments.** When the author replies to one of Redink's inline comments, Redink can concede, clarify, defend, or escalate — up to 3 rounds before handing off to a human.
- **Pluggable engines + models.** Pick per-review from the CLI or Slack:
  - `ollama` — local open-source models (`gemma4:e2b` default, `gemma3:12b`, etc.). No data leaves the box.
  - `claude-code` — shells out to the `claude` CLI in headless mode. Uses your existing Claude Code credentials. Frontier quality.

---

## 1 · One-time setup

### Requirements
- macOS / Linux
- Python 3.12+
- Docker (optional — only for the Compose deploy path)
- One or both engines:
  - **Ollama** — `brew install ollama` then `ollama pull gemma4:e2b`
  - **Claude Code** — `npm i -g @anthropic-ai/claude-code`, then `claude` once to authenticate
- GitHub Personal Access Token with `repo` scope (fine-grained or classic)
- Slack workspace + bot (optional; needed for clarification threads)
- Atlassian API token (optional; needed for Jira / Confluence context)

### Bootstrap

```bash
git clone <repo-url> redink
cd redink
./install-redink        # venv + install + .env + (optional) docker + ollama pull
source .venv/bin/activate
redink doctor           # verify every component is green
```

`install-redink` is idempotent — safe to re-run after editing `.env`.

### Configure `.env`

```bash
# ---- Engine default (override per-review with --engine / --model) ----
REDINK_ENGINE=ollama           # or: claude-code
REDINK_MODEL=                  # empty = use the engine's built-in default

# Ollama
OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=gemma4:e2b

# Claude Code (no API key needed — the CLI uses your Claude Code auth)
CLAUDE_CODE_BINARY=claude
CLAUDE_CODE_MODEL=claude-sonnet-4-6
CLAUDE_CODE_EFFORT=high        # low | medium | high | max

# ---- GitHub ----
GITHUB_PAT=ghp_xxx             # or configure GITHUB_APP_* for a GitHub App

# ---- Slack (optional) ----
SLACK_BOT_TOKEN=xoxb-...        # Bot User OAuth Token
SLACK_SIGNING_SECRET=...        # for webhook signature verification
SLACK_APP_TOKEN=xapp-...        # App-Level Token (connections:write) for Socket Mode
SLACK_REVIEW_CHANNEL=redink-reviews

# ---- Atlassian (optional) ----
ATLASSIAN_EMAIL=you@company.com
ATLASSIAN_API_TOKEN=ATATT3xFfGF0...
JIRA_BASE_URL=https://yourtenant.atlassian.net
CONFLUENCE_BASE_URL=https://yourtenant.atlassian.net
```

Generate the Atlassian token at <https://id.atlassian.com/manage-profile/security/api-tokens>.

### Start the services

```bash
redink up               # starts redink-api (FastAPI) in the background
# In a second terminal, for Slack slash commands / thread replies:
redink-slack            # Socket-Mode listener; stays in foreground
```

Stop with `redink down`.

---

## 2 · Reviewing a PR

### CLI

```bash
# Default engine + model (from .env)
redink review https://github.com/org/repo/pull/42

# Pick engine + model per run
redink review https://github.com/org/repo/pull/42 -e ollama      -m gemma4:e2b
redink review https://github.com/org/repo/pull/42 -e ollama      -m gemma3:12b
redink review https://github.com/org/repo/pull/42 -e claude-code -m claude-sonnet-4-6
redink review https://github.com/org/repo/pull/42 -e claude-code -m claude-opus-4-5

# Fire-and-forget: submit and exit (no spinner)
redink review https://github.com/org/repo/pull/42 --no-watch
```

### Slack

In your `#redink-reviews` channel:

```text
/review-pr https://github.com/org/repo/pull/42
/review-pr https://github.com/org/repo/pull/42 engine=ollama model=gemma4:e2b
/review-pr https://github.com/org/repo/pull/42 engine=claude-code model=claude-sonnet-4-6
```

Redink's root reply shows the exact engine + model in use:

```
:mag: Redink is reviewing https://github.com/org/repo/pull/42
> engine=`claude-code`  model=`claude-sonnet-4-6`  session=`89d4e645-…`
I'll post updates in this thread.
```

Progress pings, clarification questions, and the review-complete message all post in the same thread.

### Checking status / answering clarification

```bash
redink status <session-id>                  # snapshot (incl. pending questions)
redink status <session-id> --watch          # stream until terminal
redink answer <session-id> --text "The new field is optional for back-compat"
```

Equivalent on Slack: reply in the Redink thread.

---

## 3 · What runs where

```
┌──────────────┐  ┌──────────────┐  ┌─────────┐
│ Slack Bolt   │  │ CLI          │  │ MCP     │
│ (socket-mode)│  │ (typer)      │  │         │
└──────┬───────┘  └──────┬───────┘  └────┬────┘
       └──────────────┬──┴───────────────┘
                      ▼
            ┌────────────────────┐
            │ redink-api         │  FastAPI + SQLite/Postgres + in-process FSM
            └─────────┬──────────┘
          ┌───────────┼───────────┐
          ▼           ▼           ▼
   Context providers  ReviewEngine   Posters
   (repo / jira /    (ollama |     (github_poster,
    confluence /      claude-code)  slack_poster)
    linked_issues)
```

Per-PR state machine:

```
INGEST → GATHER_CONTEXT → EVALUATE_CONTEXT
                              ↓ (insufficient, ≤3 rounds)
                          AWAIT_SLACK_CLARIFICATION ─┐
                              ↓ (Slack reply) ◀──────┘
                         EVALUATE_CONTEXT
                              ↓ (sufficient)
                           REVIEW → POST → MONITORING
                                            ↓ (author replies on a comment)
                                   AWAIT_COMMENT_REPLY → ENGAGE_ON_COMMENT
                                            ↓ (≤3 rounds)
                                        DONE
```

`AWAIT_*` states are **passive DB rows** — resume is driven by Slack / GitHub webhooks, not a sleeping worker.

---

## 4 · Choosing an engine

| When | Engine | Why |
|---|---|---|
| Daily default, secure repo, cost-sensitive | `ollama` + `gemma4:e2b` | Fast, free, offline. Conservative — best for smoke review. |
| Want substantive findings + ticket-aware questions | `claude-code` + `claude-sonnet-4-6` | Frontier quality. Asks senior-reviewer-level questions. Costs ~cents / PR. |
| Deepest review on critical PRs | `claude-code` + `claude-opus-4-5` | Slow + expensive, highest ceiling. |

Set `REDINK_ENGINE` + `REDINK_MODEL` in `.env` for your default; override per-review with `-e` / `-m` on the CLI or `engine=` / `model=` on Slack.

---

## 5 · Development

```bash
git clone <repo-url> redink && cd redink
./install-redink
source .venv/bin/activate
pytest
```

Tail live logs while a review runs:

```bash
tail -f .redink-api.log
```

---

## 6 · Full reference

- **[docs/cli.md](docs/cli.md)** — every CLI command, flag, and example.
- **[docs/slack.md](docs/slack.md)** — every Slack command and thread behaviour.

Or from the terminal: `redink help` opens the CLI reference in your pager, `redink guide` prints a short cheat sheet, and `/review-pr help` in Slack prints the slash-command usage.

---

## License

Apache-2.0.
