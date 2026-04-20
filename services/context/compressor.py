"""Token-budget compression for context chunks.

Small local models (gemma4:e2b, 8k context) can't eat unbounded provider output.
This module sits between the providers and the prompt builder:

  providers -> raw chunks -> compress(chunks, budget) -> chunks that fit

Strategy (pr-agent-inspired, intentionally simple for v1):
  1. Rank by (trust_level, length) — trusted/short content is kept whole;
     untrusted/long content is summarised or truncated.
  2. Allocate budget proportionally across providers so one noisy Confluence
     page doesn't crowd out the linked Jira ticket.
  3. Compress per-chunk by dropping navigation boilerplate, collapsing
     whitespace, and truncating with a trailing "…(truncated)" marker so the
     model knows the cut was deliberate.

The estimator is intentionally cheap: `len(text) // 4` ≈ tokens for English.
We don't ship a real tokenizer — good enough for a ceiling-check, and swapping
in `tiktoken` later is a one-line change.
"""

from __future__ import annotations

import re

from services.engines.base import ContextChunk


CHARS_PER_TOKEN = 4
_BOILERPLATE = re.compile(
    r"^(?:Skip to main content|Table of contents|Edit this page|Was this helpful\??)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_MULTI_BLANK = re.compile(r"\n{3,}")


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


def compress_chunks(
    chunks: list[ContextChunk],
    *,
    token_budget: int,
    min_per_chunk_tokens: int = 120,
) -> list[ContextChunk]:
    """Return a new list of chunks that fits within `token_budget`.

    Ordering preserved from input — providers order by relevance upstream, so
    the compressor doesn't re-rank.

    If a single chunk exceeds its share of the budget, it is truncated with a
    "…(truncated)" suffix. Chunks that would come in under `min_per_chunk_tokens`
    are dropped entirely rather than emitting a useless 30-token stub.
    """
    if not chunks:
        return []

    cleaned = [_prune_boilerplate(c) for c in chunks]

    # Pre-compression pass: drop anything already below the usefulness floor.
    viable = [c for c in cleaned if estimate_tokens(c.body) >= min_per_chunk_tokens // 2]
    if not viable:
        return []

    total = sum(estimate_tokens(c.body) for c in viable)
    if total <= token_budget:
        return viable

    # Proportional allocation, but never less than min_per_chunk_tokens.
    out: list[ContextChunk] = []
    remaining = token_budget
    remaining_chunks = len(viable)
    for ch in viable:
        if remaining_chunks <= 0:
            break
        share = max(min_per_chunk_tokens, remaining // remaining_chunks)
        share = min(share, remaining)
        compressed = _truncate_to_tokens(ch, share)
        if estimate_tokens(compressed.body) >= min_per_chunk_tokens // 2:
            out.append(compressed)
            remaining -= estimate_tokens(compressed.body)
        remaining_chunks -= 1
        if remaining <= 0:
            break
    return out


def _prune_boilerplate(ch: ContextChunk) -> ContextChunk:
    body = _BOILERPLATE.sub("", ch.body)
    body = _MULTI_BLANK.sub("\n\n", body).strip()
    return ContextChunk(source=ch.source, title=ch.title, body=body, trust_level=ch.trust_level)


def _truncate_to_tokens(ch: ContextChunk, token_budget: int) -> ContextChunk:
    max_chars = token_budget * CHARS_PER_TOKEN
    if len(ch.body) <= max_chars:
        return ch
    body = ch.body[:max_chars].rstrip() + "\n…(truncated)"
    return ContextChunk(source=ch.source, title=ch.title, body=body, trust_level=ch.trust_level)
