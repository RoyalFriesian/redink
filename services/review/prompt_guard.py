"""Untrusted-content boundary for prompts.

Every bit of external content we pass to an LLM — PR body, commit messages, issue
descriptions, Confluence pages, human replies on review comments — is hostile
until proven otherwise. A motivated attacker seeds prompt-injection in any of
those fields and tries to make our reviewer either ignore real bugs or
fabricate fake ones.

This module does three things, in order, before content lands in a prompt:

1. **Strip obvious injection triggers** (fenced blocks that impersonate system
   messages, "ignore previous instructions" variants, tool-call shapes).
2. **Neutralise tag-closure attacks** — if the untrusted body contains
   `</untrusted>` we escape it so it can't punch out of our own tagging.
3. **Wrap in `<untrusted name="...">` tags** that the system prompt tells the
   model to treat as data, not instructions.

Keep this module pure: no network, no settings lookups, no logging side effects.
It runs on every prompt build.
"""

from __future__ import annotations

import re

_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    # "Ignore previous / prior / above instructions ..." family.
    re.compile(r"(?is)\bignore\s+(?:all\s+)?(?:previous|prior|above|earlier)\s+instructions?\b.*?(?:\n|$)"),
    # "Disregard the system prompt" / "override your rules".
    re.compile(r"(?is)\b(?:disregard|override|forget)\s+(?:the\s+)?(?:system|developer|assistant)\s+(?:prompt|instructions?|rules?)\b.*?(?:\n|$)"),
    # Fake role headers used to impersonate system/assistant turns.
    re.compile(r"(?im)^\s*(?:system|assistant|developer)\s*:\s*.*$"),
    # Markdown-fenced system/tool call blocks.
    re.compile(r"(?is)```(?:system|tool|assistant)\b.*?```"),
    # Attempts to smuggle in our own structured-output schema / tool shape.
    re.compile(r"(?is)<\s*(?:system|tool_call|function_call)\b[^>]*>.*?<\s*/\s*(?:system|tool_call|function_call)\s*>"),
)

_MAX_CONSECUTIVE_BLANK_LINES = 2


def sanitize(text: str) -> str:
    """Return `text` with injection patterns stripped and tag closures escaped.

    Never raises; worst case returns an empty string for input that was entirely
    made of injection markers.
    """
    if not text:
        return ""

    out = text
    for pat in _INJECTION_PATTERNS:
        out = pat.sub(" [redacted-injection] ", out)

    # Prevent the untrusted body from punching out of its own wrapper.
    out = out.replace("</untrusted>", "&lt;/untrusted&gt;")
    out = out.replace("<untrusted", "&lt;untrusted")

    # Collapse runs of blank lines so a redaction spree doesn't produce a
    # 500-line prompt of whitespace.
    out = re.sub(r"\n{%d,}" % (_MAX_CONSECUTIVE_BLANK_LINES + 1), "\n" * _MAX_CONSECUTIVE_BLANK_LINES, out)
    return out.strip()


def wrap(text: str, *, name: str, max_chars: int | None = None) -> str:
    """Sanitize and wrap `text` in an `<untrusted name="...">` block.

    `name` identifies the source in the prompt (e.g. "pr_body", "jira_ABC-123",
    "confluence_page_42"). `max_chars` truncates before wrapping so the
    sanitiser doesn't spend cycles on content we'd drop anyway.
    """
    body = text[:max_chars] if max_chars else text
    body = sanitize(body)
    return f'<untrusted name="{name}">\n{body}\n</untrusted>'
