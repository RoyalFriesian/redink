"""`ContextProvider` protocol + registry.

A provider takes the PR under review and the set of references it can find
(ticket ids, doc URLs, adjacent repo paths) and returns a list of
`ContextChunk` rows. Providers are independent — one slow/broken provider
cannot stop the review from running.

Every provider output is **untrusted** by default: we treat Jira descriptions
and Confluence pages as attacker-controlled input. The `RepoDocs` provider is
the one exception that may legitimately mark content as `trusted`, since repo
files are governed by the same CODEOWNERS that govern the reviewed code.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Protocol

from services.engines.base import ContextChunk

log = logging.getLogger(__name__)


@dataclass
class PRRefs:
    """References extracted from the PR text that providers can resolve.

    Populated by `extract_refs` in `linked_issues.py`. Keeping this dataclass
    separate from the raw PR bundle means providers don't each re-parse the
    PR body.
    """

    pr_url: str
    repo_slug: str
    title: str
    body: str
    branch_name: str
    jira_keys: list[str]
    github_issues: list[int]
    urls: list[str]


class ContextProvider(Protocol):
    name: str

    def is_enabled(self) -> bool:
        """Return False to silently skip this provider (missing creds, etc.)."""

    def fetch(self, refs: PRRefs) -> list[ContextChunk]:
        """Return chunks for this provider. Must not raise; return [] on failure."""


ProgressCallback = Callable[[str], None]


def gather(
    providers: list[ContextProvider],
    refs: PRRefs,
    *,
    on_progress: ProgressCallback | None = None,
) -> list[ContextChunk]:
    """Run every enabled provider and flatten results.

    `on_progress(text)` is invoked before each enabled provider runs and after
    it returns — the caller can surface live status into Slack so reviewers
    aren't staring at silence while we fetch Jira / Confluence / the repo.

    Exceptions are swallowed per-provider with a warning — a dead Confluence
    integration shouldn't block the GitHub review.
    """
    out: list[ContextChunk] = []
    for p in providers:
        if not p.is_enabled():
            log.debug("provider %s disabled; skipping", p.name)
            continue
        if on_progress:
            on_progress(_start_label(p, refs))
        try:
            chunks = p.fetch(refs)
            log.info("provider %s returned %d chunks", p.name, len(chunks))
            if on_progress:
                on_progress(f":white_check_mark: `{p.name}` → {len(chunks)} chunk(s)")
            out.extend(chunks)
        except Exception:  # broad by design — providers must never kill the review
            log.exception("provider %s failed", p.name)
            if on_progress:
                on_progress(f":warning: `{p.name}` failed — skipping")
    return out


def _start_label(p: ContextProvider, refs: PRRefs) -> str:
    """A one-line 'what am I doing' message tailored to the provider."""
    name = p.name
    if name == "repo_snapshot":
        return f":books: Indexing repo snapshot for `{refs.repo_slug}`"
    if name == "github_linked_issues":
        issues = ", ".join(f"#{n}" for n in refs.github_issues) or "(none linked)"
        return f":link: Resolving GitHub issues: {issues}"
    if name == "jira":
        keys = ", ".join(f"`{k}`" for k in refs.jira_keys) or "(none)"
        return f":jira: Fetching Jira tickets: {keys}"
    if name == "confluence":
        return ":page_facing_up: Scanning Confluence links from the PR"
    return f":hourglass_flowing_sand: Running provider `{name}`"
