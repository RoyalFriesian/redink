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
                links = _reference_links(p.name, chunks)
                # Append a short `• links: …` tail for jira/confluence so
                # reviewers can click through to whatever Redink actually
                # pulled. Other providers don't get links (repo snapshot is
                # the whole repo; github issues are already in the PR body).
                suffix = f"  · {links}" if links else ""
                on_progress(
                    f":white_check_mark: `{p.name}` → {len(chunks)} chunk(s){suffix}"
                )
            out.extend(chunks)
        except Exception:  # broad by design — providers must never kill the review
            log.exception("provider %s failed", p.name)
            if on_progress:
                on_progress(f":warning: `{p.name}` failed — skipping")
    return out


def _reference_links(provider_name: str, chunks: list[ContextChunk]) -> str:
    """Build a Slack-formatted `<url|label>` list from the chunks, for the
    providers where a clickable link actually helps the reviewer — i.e. Jira
    tickets and Confluence pages. Returns an empty string otherwise.

    We deliberately dedupe Jira keys so that (KEY, KEY:comments, KEY:subtask:X)
    collapses to one entry per distinct ticket. Confluence chunks each have
    their own page id, so every chunk gets its own link.
    """
    from services.config import settings

    if not chunks:
        return ""
    s = settings()

    if provider_name == "jira":
        base = (s.jira_base_url or "").rstrip("/")
        if not base:
            return ""
        keys: list[str] = []
        seen: set[str] = set()
        for c in chunks:
            # source shapes: "jira:KEY", "jira:KEY:comments",
            # "jira:KEY:subtask:CHILD", "jira:KEY:parent:EPIC".
            parts = (c.source or "").split(":")
            if len(parts) >= 2 and parts[0] == "jira":
                key = parts[3] if len(parts) >= 4 and parts[2] in ("subtask", "parent") else parts[1]
                if key and key not in seen:
                    seen.add(key)
                    keys.append(key)
        if not keys:
            return ""
        return "links: " + ", ".join(f"<{base}/browse/{k}|{k}>" for k in keys)

    if provider_name == "confluence":
        base = (s.confluence_base_url or "").rstrip("/")
        if not base:
            return ""
        # Normalise so we don't double up on `/wiki` whether the user configured
        # `https://tenant.atlassian.net` or `https://tenant.atlassian.net/wiki`.
        if base.endswith("/wiki"):
            wiki = base
        else:
            wiki = f"{base}/wiki"
        links: list[str] = []
        seen_ids: set[str] = set()
        for c in chunks:
            parts = (c.source or "").split(":", 1)
            if len(parts) == 2 and parts[0] == "confluence":
                pid = parts[1]
                if pid and pid not in seen_ids:
                    seen_ids.add(pid)
                    label = (c.title or f"page {pid}").replace("|", "-").replace(">", "")[:60]
                    links.append(f"<{wiki}/pages/viewpage.action?pageId={pid}|{label}>")
        if not links:
            return ""
        return "links: " + ", ".join(links)

    return ""


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
