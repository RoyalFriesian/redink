"""Pre-LLM and pre-outbound secret scrubber.

Runs in two places:
  1. Before every engine call — we never want a leaked AWS key in the diff to
     be sent to an external model (Anthropic/OpenAI); for local Ollama it's
     still prudent (logs, audit table, multi-tenant future).
  2. Before every outbound post to Slack or GitHub — a hallucinated reviewer
     comment that quotes a secret must not be re-broadcast.

Strategy:
  - Cheap regex pass for the ~15 highest-signal credential shapes (AWS, GCP,
    GitHub PAT, Slack, Stripe, private-key PEM, JWT, generic "KEY=...").
  - Optional `detect-secrets` pass when the library is installed — catches the
    long tail (high-entropy strings, hex blobs) without baking every rule here.

Both layers replace the match with `«redacted:<kind>»` so the content stays
human-readable and a reviewer seeing a scrubbed comment understands why.

Keep the fast path allocation-free for short strings — this runs on every diff
chunk and every outbound comment body.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

_REDACT = "«redacted:{kind}»"


@dataclass(frozen=True)
class _Rule:
    kind: str
    pattern: re.Pattern[str]


_RULES: tuple[_Rule, ...] = (
    _Rule("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    _Rule(
        "aws-secret-key",
        re.compile(r"(?i)aws(.{0,20})?(secret|private).{0,20}?['\"]([A-Za-z0-9/+=]{40})['\"]"),
    ),
    _Rule("gcp-service-account", re.compile(r"\"type\":\s*\"service_account\"")),
    _Rule("github-pat", re.compile(r"\bghp_[A-Za-z0-9]{36,}\b")),
    _Rule("github-oauth", re.compile(r"\bgho_[A-Za-z0-9]{36,}\b")),
    _Rule("github-app-token", re.compile(r"\b(?:ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b")),
    _Rule("slack-token", re.compile(r"\bxox[abpors]-[A-Za-z0-9-]{10,}\b")),
    _Rule("stripe-key", re.compile(r"\b(?:sk|rk)_(?:test|live)_[A-Za-z0-9]{20,}\b")),
    _Rule("openai-key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    _Rule("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9-_]{20,}\b")),
    _Rule("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    _Rule(
        "private-key-pem",
        re.compile(
            r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----.*?-----END (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    _Rule(
        "kv-password",
        re.compile(
            r"(?i)\b(?:password|passwd|secret|api[_-]?key|token|auth[_-]?token)\s*[:=]\s*['\"]([^'\"\s]{8,})['\"]"
        ),
    ),
)


def scrub(text: str) -> str:
    """Regex-scrub well-known credential shapes. Always returns a string.

    Idempotent: running it twice produces the same output as once.
    """
    if not text:
        return text or ""
    out = text
    for rule in _RULES:
        out = rule.pattern.sub(_REDACT.format(kind=rule.kind), out)
    return out


def scrub_deep(text: str) -> str:
    """Regex pass + optional `detect-secrets` pass for high-entropy strings.

    `detect-secrets` is an optional dep — if it's not installed we silently fall
    back to the regex-only path so tests and dev installs don't hard-require it.
    """
    out = scrub(text)
    try:
        from detect_secrets.core.scan import scan_line  # type: ignore
    except ImportError:  # pragma: no cover - optional dep
        return out

    try:
        scrubbed_lines: list[str] = []
        for line in out.splitlines():
            hit = next(iter(scan_line(line)), None)
            if hit is None:
                scrubbed_lines.append(line)
                continue
            # scan_line returns PotentialSecret; mask the matched secret_value range
            secret = getattr(hit, "secret_value", None)
            if secret and secret in line:
                line = line.replace(secret, _REDACT.format(kind="entropy"))
            scrubbed_lines.append(line)
        return "\n".join(scrubbed_lines)
    except Exception:  # pragma: no cover - detect-secrets should never kill the flow
        log.debug("detect-secrets failed; falling back to regex scrub", exc_info=True)
        return out


def scrub_outbound(text: str) -> str:
    """Alias used at the outbound boundary (GH / Slack posters).

    A separate name makes callsites self-documenting and gives us a hook point
    if outbound scrubbing ever needs different rules from inbound.
    """
    return scrub(text)
