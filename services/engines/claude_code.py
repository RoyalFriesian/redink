"""Claude Code engine — drives the review via the `claude` CLI in headless mode.

The orchestrator already has well-tuned prompts (`services.review.prompts`)
and validated output schemas (`services.review.schemas`). Rather than invent
a separate toolbelt-driven flow, this engine reuses both: for each engine
task (`evaluate_context`, `review`, `engage_on_reply`) we shell out to
`claude -p --output-format=json --model=<x> --effort=<y>`, strip the CLI's
result envelope, and feed the model's JSON into the same pydantic validator
the Ollama engine uses. Same prompts → directly comparable results in the
eval harness.

Tools are disabled end-to-end (`--disallowed-tools Bash Edit Write Read ...`)
so the CLI does pure reasoning — no accidental filesystem writes, no
network calls we didn't sanction.

Credentials: the `claude` CLI uses the developer's existing Claude Code
auth. No `ANTHROPIC_API_KEY` is required.
"""

from __future__ import annotations

import json
import logging
import shlex
import subprocess
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from services.config import settings
from services.engines.base import (
    ClarificationQuestion,
    ContextEvaluation,
    Finding,
    ReviewContext,
    ReviewEngine,
)
from services.engines.ollama import _parse_json_loose
from services.review.prompts import (
    build_engage_prompt,
    build_evaluate_prompt,
    build_review_prompt,
)
from services.review.schemas import (
    ContextEvaluationOut,
    EngageActionOut,
    FileReviewOut,
)
from services.review.secret_scrubber import scrub

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# Every built-in Claude Code tool — we explicitly deny them all so the review
# pass is pure reasoning. Adding a new tool in a future CLI release should
# default to denied; we'll widen the list on a case-by-case basis.
_DISALLOWED_TOOLS = (
    "Bash Edit Write Read Glob Grep WebFetch WebSearch Task "
    "TodoWrite NotebookEdit"
)


class ClaudeCodeEngine(ReviewEngine):
    name = "claude-code"

    def __init__(
        self,
        binary: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        timeout_s: float | None = None,
        max_retries: int = 2,
    ) -> None:
        s = settings()
        self._binary = binary or s.claude_code_binary
        self._model = model or s.claude_code_model
        self._effort = effort or s.claude_code_effort
        self._timeout = timeout_s or s.claude_code_timeout_s
        self._max_retries = max_retries

    # ---- ReviewEngine surface --------------------------------------------------------

    def evaluate_context(self, ctx: ReviewContext) -> ContextEvaluation:
        system, user = build_evaluate_prompt(ctx)
        parsed = self._generate_json_with_retry(
            system,
            user,
            ContextEvaluationOut,
            fallback=ContextEvaluationOut(
                reasoning="engine fell back — could not parse model output",
                sufficient=True,
                questions=[],
            ),
            purpose="evaluate_context",
        )
        log.info(
            "evaluate_context reasoning (round %d): %s",
            len(ctx.rounds) + 1,
            (parsed.reasoning or "(none)").replace("\n", " ")[:500],
        )
        questions = [
            ClarificationQuestion(id=q.id, text=q.text, why_needed=q.why_needed)
            for q in parsed.questions
        ]
        return ContextEvaluation(
            sufficient=parsed.sufficient,
            questions=questions,
            reasoning=parsed.reasoning,
        )

    def review(self, ctx: ReviewContext, *, on_progress=None) -> list[Finding]:
        findings: list[Finding] = []
        reviewable = [
            f
            for f in ctx.files
            if (f.get("filename") or f.get("path"))
            and f.get("patch")
            and not _should_skip(f.get("filename") or f.get("path") or "")
        ]
        total = len(reviewable)
        for i, f in enumerate(reviewable, start=1):
            path = f.get("filename") or f.get("path") or ""
            patch = f.get("patch") or ""
            if on_progress:
                on_progress(f":pencil2: Reviewing `{path}` ({i}/{total})")

            system, user = build_review_prompt(ctx, path=path, patch=patch)
            parsed = self._generate_json_with_retry(
                system,
                user,
                FileReviewOut,
                fallback=FileReviewOut(path=path, findings=[]),
                purpose=f"review::{path}",
            )

            valid_lines = _added_line_numbers(patch)
            kept_for_file = 0
            for finding in parsed.findings:
                if finding.line not in valid_lines:
                    log.debug("dropping hallucinated line %s in %s", finding.line, path)
                    continue
                findings.append(
                    Finding(
                        path=finding.path or path,
                        line=finding.line,
                        severity=finding.severity,
                        body=finding.body,
                    )
                )
                kept_for_file += 1
            if on_progress and kept_for_file:
                on_progress(f"    → `{path}`: {kept_for_file} finding(s)")
        return findings

    def engage_on_reply(
        self, finding: Finding, reply_text: str, ctx: ReviewContext
    ) -> tuple[str, str]:
        patch = _find_patch(ctx.files, finding.path)
        system, user = build_engage_prompt(finding.body, reply_text, patch)
        parsed = self._generate_json_with_retry(
            system,
            user,
            EngageActionOut,
            fallback=EngageActionOut(
                action="escalate",
                body="Redink couldn't produce a confident response; flagging to a human.",
            ),
            purpose="engage_on_reply",
        )
        return parsed.action, parsed.body

    # ---- Claude Code plumbing --------------------------------------------------------

    def _generate_json_with_retry(
        self,
        system: str,
        user: str,
        schema: type[T],
        *,
        fallback: T,
        purpose: str,
    ) -> T:
        system = scrub(system)
        user = scrub(user)
        last_err: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                raw = self._generate_json(system, user, schema)
                return schema.model_validate(raw)
            except (
                ValidationError,
                json.JSONDecodeError,
                subprocess.TimeoutExpired,
                subprocess.CalledProcessError,
                ValueError,
            ) as exc:
                last_err = exc
                log.warning(
                    "claude-code %s attempt %d/%d failed: %s",
                    purpose,
                    attempt + 1,
                    self._max_retries + 1,
                    exc,
                )
        log.error(
            "claude-code %s exhausted retries (%s); using fallback",
            purpose,
            last_err,
        )
        return fallback

    def _generate_json(self, system: str, user: str, schema: type[BaseModel]) -> dict:
        # Pass the prompt on stdin — it can be multi-megabyte (fat diffs +
        # context), which would blow argv limits if we passed it as a flag.
        # Stdout is the CLI's JSON envelope; stderr is human diagnostics.
        prompt = (
            f"{system}\n\n{user}\n\n"
            "Respond with ONLY the JSON object that satisfies the schema. "
            "No prose, no markdown fences."
        )
        cmd = [
            self._binary,
            "-p",
            "--output-format", "json",
            "--model", self._model,
            "--effort", self._effort,
            "--no-session-persistence",
            "--disallowed-tools", _DISALLOWED_TOOLS,
        ]
        log.debug("claude-code cmd: %s", shlex.join(cmd))
        proc = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=self._timeout,
            check=False,
        )
        if proc.returncode != 0:
            raise subprocess.CalledProcessError(
                proc.returncode, cmd, output=proc.stdout, stderr=proc.stderr,
            )
        out = proc.stdout.strip()
        if not out:
            raise ValueError("empty stdout from claude CLI")
        envelope = json.loads(out)
        if envelope.get("is_error"):
            raise ValueError(f"claude CLI reported error: {envelope.get('subtype')}")
        result = envelope.get("result") or ""
        if not result.strip():
            raise ValueError("empty `result` in claude CLI envelope")
        # Re-use Ollama's loose parser — the CLI sometimes wraps `result` in
        # ```json fences even when we ask for raw JSON.
        return _parse_json_loose(result)


# ---------------------------------------------------------------- helpers
# (Copied locally rather than re-imported from ollama.py to keep the two
# engines independent; they're trivial and shouldn't grow.)


_SKIP_PATTERNS = (
    "node_modules/",
    "vendor/",
    "dist/",
    "build/",
    ".min.js",
    ".min.css",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "Cargo.lock",
    "go.sum",
)


def _should_skip(path: str) -> bool:
    p = path.lower()
    return any(pat in p for pat in _SKIP_PATTERNS)


def _added_line_numbers(patch: str) -> set[int]:
    out: set[int] = set()
    cur = 0
    for line in patch.splitlines():
        if line.startswith("@@"):
            try:
                hunk = line.split("+", 1)[1].split(" ")[0]
                cur = int(hunk.split(",")[0])
            except (IndexError, ValueError):
                continue
        elif line.startswith("+") and not line.startswith("+++"):
            out.add(cur)
            cur += 1
        elif line.startswith("-") and not line.startswith("---"):
            continue
        else:
            cur += 1
    return out


def _find_patch(files: list[dict], path: str) -> str:
    for f in files:
        if (f.get("filename") or f.get("path")) == path:
            return f.get("patch") or ""
    return ""
