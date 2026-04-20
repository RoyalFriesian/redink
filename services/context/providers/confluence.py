"""Confluence page provider — explicit URLs + CQL search.

Two ways a page enters the review context:

  1. **Explicit link.** The PR body references an `atlassian.net/wiki/...`
     URL. We resolve it via `/wiki/api/v2/pages/{id}`.
  2. **CQL search.** Even when the author didn't link a doc, there is often
     a design page that explains the ticket. We build a seed query from the
     PR title + referenced Jira keys and call `/wiki/rest/api/content/search`
     with a `text ~ "..."` CQL expression, taking the top few hits.

Both paths feed the same ADF flattener (`_flatten_adf`, re-used from the Jira
provider). All chunks are `trust_level="untrusted"` — Confluence pages are
attacker-editable in most orgs.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import parse_qs, urlparse

import httpx

from services.context.providers.base import PRRefs
from services.context.providers.jira import _flatten_adf
from services.engines.base import ContextChunk

log = logging.getLogger(__name__)

_MODERN = re.compile(r"/wiki/spaces/[^/]+/pages/(\d+)")

# Caps — search is broader than explicit links, keep the footprint modest.
_MAX_EXPLICIT = 5
_MAX_SEARCH = 3
# Strip generic English fragments so the CQL query latches onto domain terms
# (ticket keys, module names) rather than stop-word noise.
_STOP_WORDS = {
    "a", "an", "the", "and", "or", "for", "to", "of", "in", "on", "with",
    "fix", "add", "update", "remove", "refactor", "feat", "chore", "bug",
    "pr", "issue", "pull", "request", "via", "from", "into",
}


class ConfluenceProvider:
    name = "confluence"

    def is_enabled(self) -> bool:
        from services.config import settings

        s = settings()
        return bool(s.confluence_base_url and s.confluence_auth())

    def fetch(self, refs: PRRefs) -> list[ContextChunk]:
        from services.config import settings

        s = settings()
        base = s.confluence_base_url.rstrip("/")
        host = urlparse(base).netloc
        auth = s.confluence_auth()
        if auth is None:
            return []

        chunks: list[ContextChunk] = []
        fetched_ids: set[str] = set()

        explicit_ids = _page_ids_from_urls(refs.urls, host=host)
        with httpx.Client(timeout=30.0) as c:
            for pid in explicit_ids[:_MAX_EXPLICIT]:
                chunk = self._fetch_page(c, base, auth, pid)
                if chunk:
                    chunks.append(chunk)
                    fetched_ids.add(pid)

            query = _build_cql_seed(refs)
            if query:
                for pid, title in self._cql_search(c, base, auth, query):
                    if pid in fetched_ids:
                        continue
                    chunk = self._fetch_page(c, base, auth, pid, fallback_title=title)
                    if chunk:
                        chunks.append(chunk)
                        fetched_ids.add(pid)
                    if len(fetched_ids) >= _MAX_EXPLICIT + _MAX_SEARCH:
                        break
        return chunks

    def _fetch_page(
        self,
        client: httpx.Client,
        base: str,
        auth: tuple[str, str],
        pid: str,
        *,
        fallback_title: str = "",
    ) -> ContextChunk | None:
        try:
            r = client.get(
                f"{base}/wiki/api/v2/pages/{pid}",
                auth=auth,
                params={"body-format": "atlas_doc_format"},
            )
            if r.status_code == 404:
                return None
            r.raise_for_status()
            page = r.json()
        except Exception:
            log.exception("confluence fetch failed for page %s", pid)
            return None
        title = page.get("title") or fallback_title or f"page {pid}"
        adf = ((page.get("body") or {}).get("atlas_doc_format") or {}).get("value")
        body = _flatten_adf(adf) if adf else ""
        return ContextChunk(
            source=f"confluence:{pid}",
            title=title,
            body=f"{title}\n\n{body}".strip(),
            trust_level="untrusted",
        )

    def _cql_search(
        self,
        client: httpx.Client,
        base: str,
        auth: tuple[str, str],
        query: str,
    ) -> list[tuple[str, str]]:
        # v1 search endpoint — the v2 equivalent doesn't yet accept raw CQL on
        # all Cloud tiers, so we stick with the stable REST v1 route.
        cql = f'type = "page" AND text ~ "{query}"'
        try:
            r = client.get(
                f"{base}/wiki/rest/api/content/search",
                auth=auth,
                params={"cql": cql, "limit": _MAX_SEARCH * 2},
            )
            r.raise_for_status()
            results = (r.json() or {}).get("results") or []
        except Exception:
            log.exception("confluence CQL search failed for %r", query)
            return []
        hits: list[tuple[str, str]] = []
        for item in results:
            pid = item.get("id")
            if not pid:
                continue
            hits.append((str(pid), item.get("title") or ""))
        return hits


def _build_cql_seed(refs: PRRefs) -> str:
    """Build a CQL `text ~` expression from the PR title + Jira keys.

    Jira keys (`AR-2909`) are the most specific signal — they usually surface
    the exact design page on the first hit. Title tokens are appended as a
    looser fallback for PRs that don't reference a ticket.
    """
    terms: list[str] = []
    for key in refs.jira_keys:
        if key not in terms:
            terms.append(key)
    for tok in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", refs.title or ""):
        low = tok.lower()
        if low in _STOP_WORDS:
            continue
        if tok not in terms:
            terms.append(tok)
    if not terms:
        return ""
    # CQL `text ~ "a b c"` is a phrase-ish match; space-joined terms behave
    # like an OR across the indexed text in practice.
    safe = " ".join(t.replace('"', "") for t in terms[:6])
    return safe


def _page_ids_from_urls(urls: list[str], *, host: str) -> list[str]:
    ids: list[str] = []
    for u in urls:
        try:
            parsed = urlparse(u)
        except Exception:
            continue
        if parsed.netloc != host:
            continue
        m = _MODERN.search(parsed.path)
        if m:
            ids.append(m.group(1))
            continue
        if parsed.path.endswith("/viewpage.action"):
            qs = parse_qs(parsed.query)
            if "pageId" in qs:
                ids.append(qs["pageId"][0])
    seen = set()
    uniq = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            uniq.append(i)
    return uniq
