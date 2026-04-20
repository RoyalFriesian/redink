# Redink Slack Reference

Everything you can do with Redink from Slack. For the CLI equivalent, see [cli.md](cli.md).

All Redink interaction in Slack happens in the review channel configured by `SLACK_REVIEW_CHANNEL` (default `redink-reviews`). The bot must be invited to that channel — `/invite @Redink`.

---

## Quick reference

| What you type | What happens |
|---|---|
| `/review-pr <url>` | Start a review with the default engine + model. |
| `/review-pr <url> engine=ollama model=gemma4:e2b` | Start a review with a specific engine + model. |
| `/review-pr help` | Print this usage, ephemerally. |
| *(reply in a Redink thread)* | Answer the open clarification round, or engage on a finding. |

---

## `/review-pr`

Kick off a review. Redink posts a root message in the review channel, then does all further updates (progress, clarification questions, finished message) in that thread.

### Syntax

```text
/review-pr <github-pr-url> [engine=ollama|claude-code] [model=<name>]
```

Both forms of the flags work — pick whichever feels natural:

| `key=value`                     | Long flag                    |
|---------------------------------|------------------------------|
| `engine=ollama`                 | `--engine ollama` / `-e ollama` |
| `model=gemma4:e2b`              | `--model gemma4:e2b` / `-m gemma4:e2b` |

Argument order doesn't matter. Unknown tokens raise a usage error rather than being silently ignored.

### Engine + model cheat sheet

| Engine | Models | Notes |
|---|---|---|
| `ollama` | `gemma4:e2b` (default), `gemma3:12b`, `qwen2.5-coder:7b` | Local, free, conservative. Pull with `ollama pull <model>` first. |
| `claude-code` | `claude-sonnet-4-6` (default), `claude-opus-4-5` | Frontier quality via the `claude` CLI. Costs ~cents/PR. |

If `model=` is omitted the engine's default is used — Redink shows the effective model in its root reply so there's never any ambiguity.

### Examples

```text
# Daily default
/review-pr https://github.com/org/repo/pull/42

# Force local review
/review-pr https://github.com/org/repo/pull/42 engine=ollama model=gemma4:e2b

# Frontier review
/review-pr https://github.com/org/repo/pull/42 engine=claude-code model=claude-sonnet-4-6

# Deepest review
/review-pr https://github.com/org/repo/pull/42 engine=claude-code model=claude-opus-4-5

# Long-flag form still works
/review-pr https://github.com/org/repo/pull/42 --engine claude-code -m claude-opus-4-5
```

### What Redink posts back

**Ephemeral ack** (only you see it):

```
:rocket: review started — session `89d4e645-…`
> engine=`claude-code`  model=`claude-sonnet-4-6`  status=`INGEST`
Watch #redink-reviews for updates.
```

**Public root in the review channel** (everyone sees it — the thread for everything that follows):

```
:mag: Redink is reviewing https://github.com/org/repo/pull/42
> engine=`claude-code`  model=`claude-sonnet-4-6`  session=`89d4e645-…`
I'll post updates in this thread.
```

### Errors

| Message | Fix |
|---|---|
| `:x: PR URL is required` | Pass a GitHub PR URL. |
| `:x: unknown engine ``foo`` — must be one of: claude-code, ollama` | Use `ollama` or `claude-code`. |
| `:x: unrecognised argument: ``...``` | Check for typos — e.g. `engines=` (plural) instead of `engine=`. |
| `:x: failed to start review: ...` | Redink API is down or unreachable. Run `redink up` / `redink doctor`. |

---

## Replying in a Redink thread

Redink uses the thread it opened for everything:

### Answering clarification

When the review is in `AWAIT_SLACK_CLARIFICATION`, Redink posts questions in the thread tagging the PR author. Any thread reply (by anyone in the channel) is captured as the answer. The engine parses it — you can answer in prose:

> "The new field is optional for back-compat. Downstream consumers ignore unknown keys."

You don't need to number answers or match question order. One message is enough.

Equivalent CLI: `redink answer <session-id> --text "…"`.

### Engaging on an inline comment

After the review is posted on GitHub, if the PR author replies on one of Redink's inline comments, Redink replies back on the same GitHub comment thread — not in Slack. Up to 3 rounds per comment, then it escalates to a human.

The Slack thread is only for clarification *before* the review is posted.

---

## Troubleshooting

| Symptom | Check |
|---|---|
| Nothing happens when I run `/review-pr` | Is the Socket-Mode listener running? `redink-slack` must be running alongside `redink up`. Check `SLACK_APP_TOKEN` (starts with `xapp-`). |
| `slack not configured` error | `SLACK_BOT_TOKEN` or `SLACK_SIGNING_SECRET` unset in `.env`. |
| Bot posts ack but no thread appears | Bot isn't in `SLACK_REVIEW_CHANNEL`. `/invite @Redink` in that channel. |
| Clarification reply is ignored | Reply must be *in the Redink thread*, not the channel. Also — the session must be in `AWAIT_SLACK_CLARIFICATION`. Check with `redink status <id>`. |
| "Thread is archived/locked" in logs | Slack won't let bots post in archived threads. Start a new review (`--mode restart`). |

---

See also: [CLI reference](cli.md) · [README](../README.md)
