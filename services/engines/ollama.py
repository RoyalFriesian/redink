"""Ollama engine — small-local-model reviewer (default: gemma4:e2b).

All calls go through `_generate_json_with_retry`, which:
  - uses Ollama's JSON mode (`format: "json"`) — the model is biased toward
    valid JSON but is not grammar-masked token-by-token. That was a
    deliberate trade: schema-constrained decode (`format: <schema>`) masks
    illegal tokens at every step, which is ~3-5× slower and, on gemma4:e2b
    with a fat 32k context, sometimes produced empty completions. Plain
    JSON mode + pydantic validation + a small retry budget has been faster
    and just as reliable in practice.
  - talks to `/api/chat` with separated system/user roles — small instruction-
    tuned models follow the chat template far more reliably than a flat prompt.
  - robustly parses the response: strips code fences / surrounding prose,
    extracts the first balanced `{...}` block, then feeds it to pydantic.
  - retries on validation or trim failure, falls back to a safe default
    rather than raising.

The review prompt is run **per changed file** — small models struggle to reason over
whole-PR diffs, and per-file chunking keeps each call well inside the context window.
"""

from __future__ import annotations

import json
import logging
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from services.config import settings
from services.engines.base import (
    ClarificationQuestion,
    ContextEvaluation,
    Finding,
    ReviewContext,
    ReviewEngine,
)
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


class OllamaEngine(ReviewEngine):
    name = "ollama"

    def __init__(
        self,
        host: str | None = None,
        model: str | None = None,
        max_retries: int = 2,
        request_timeout: float = 180.0,
    ) -> None:
        self._host = (host or settings().ollama_host).rstrip("/")
        self._model = model or settings().ollama_model
        # Grammar-constrained output makes syntactic failure impossible; the only
        # remaining failure mode is semantic (pydantic range/type) which almost
        # always repeats on a small model with the same prompt — one retry is
        # plenty, more just burns time.
        self._max_retries = max_retries
        self._timeout = request_timeout

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
            f for f in ctx.files
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
                # Gemma sometimes hallucinates line numbers; only keep ones that really
                # appear in the added lines of this file.
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

    # ---- Ollama plumbing -------------------------------------------------------------

    def _generate_json_with_retry(
        self,
        system: str,
        user: str,
        schema: type[T],
        *,
        fallback: T,
        purpose: str,
    ) -> T:
        # Scrub secrets out of the full prompt before it ever leaves the process.
        # prompt_guard handled injection markers; this handles credential leakage
        # from diffs, ticket descriptions, etc.
        system = scrub(system)
        user = scrub(user)
        last_err: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                raw = self._generate_json(system, user, schema)
                return schema.model_validate(raw)
            except (ValidationError, json.JSONDecodeError, httpx.HTTPError, ValueError) as exc:
                last_err = exc
                log.warning(
                    "ollama %s attempt %d/%d failed: %s",
                    purpose,
                    attempt + 1,
                    self._max_retries + 1,
                    exc,
                )
                # No prompt tightening — adding text makes 5B models worse, not
                # better. Grammar-constrained decode means the retry can only
                # help on transient network/model glitches.

        log.error("ollama %s exhausted retries (%s); using fallback", purpose, last_err)
        return fallback

    def _generate_json(self, system: str, user: str, schema: type[BaseModel]) -> dict:
        # `format: "json"` biases decoding toward valid JSON without the
        # per-token grammar mask. Combined with the pydantic validator below,
        # this has been faster and more reliable on gemma4:e2b @ 32k than
        # schema-constrained decode, which occasionally returned empty.
        resp = httpx.post(
            f"{self._host}/api/chat",
            json={
                "model": self._model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "format": "json",
                "stream": False,
                "options": {
                    "temperature": 0.0,  # deterministic decode for structured output
                    # gemma4:e2b natively supports 131k context; 8k truncated
                    # long diffs (e.g. a 20KB test file) to an empty response.
                    # 32k fits the fattest files in our corpus with headroom,
                    # without paying the RAM cost of the full 128k window.
                    "num_ctx": 32768,
                    "num_predict": 4096,
                },
            },
            timeout=self._timeout,
        )
        resp.raise_for_status()
        payload = resp.json()
        text = ((payload.get("message") or {}).get("content")) or ""
        if not text.strip():
            raise ValueError("empty response from Ollama")
        return _parse_json_loose(text)


def _parse_json_loose(text: str) -> dict:
    """Parse a JSON object out of a possibly-dirty model response.

    Small models in `format: "json"` mode usually emit clean JSON, but
    occasionally wrap it in ```json fences, prepend a sentence of prose, or
    append a trailing token stream. We:
      1. strip code fences,
      2. locate the first `{` and walk until the matching `}` — respecting
         string literals so braces inside strings don't confuse the counter,
      3. `json.loads` that slice.

    Raises `json.JSONDecodeError` if no balanced object can be extracted, so
    the caller's retry loop picks it up like any other parse failure.
    """
    s = text.strip()
    if s.startswith("```"):
        # ```json\n{...}\n```  or  ```{...}```
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:]
        s = s.strip()
    start = s.find("{")
    if start < 0:
        raise json.JSONDecodeError("no '{' in model output", s, 0)
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(s[start : i + 1])
    raise json.JSONDecodeError("unbalanced braces in model output", s, start)


def _ollama_schema(schema: type[BaseModel]) -> dict:
    """Turn a pydantic model into a JSON schema Ollama/llama.cpp will accept.

    pydantic emits a few features (`$defs`, union types with `null`, enum via
    `const` in anyOf) that llama.cpp's GBNF converter handles well for simple
    cases but can choke on for nested unions. We inline `$defs` eagerly to
    avoid per-version quirks in the grammar builder.
    """
    js = schema.model_json_schema()
    defs = js.pop("$defs", {}) or js.pop("definitions", {}) or {}
    if defs:
        _inline_refs(js, defs)
    return js


def _inline_refs(node, defs: dict) -> None:
    """Recursively replace `$ref` nodes with their inlined definition."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/"):
            key = ref.rsplit("/", 1)[-1]
            target = defs.get(key)
            if isinstance(target, dict):
                node.pop("$ref", None)
                for k, v in target.items():
                    node.setdefault(k, v)
        for v in node.values():
            _inline_refs(v, defs)
    elif isinstance(node, list):
        for item in node:
            _inline_refs(item, defs)


# ---------------------------------------------------------------- helpers


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
    """Return the set of line numbers in the NEW file that were added/modified."""
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
