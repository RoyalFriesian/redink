"""Extract linked-reference keys from a PR and pull GitHub issues.

Two responsibilities:
  - `extract_refs(bundle)` — regex-scan title + body + branch for Jira keys
    (`ABC-123`), GitHub issue numbers (`#12`, `fixes org/repo#34`), and bare
    URLs (passed to Confluence provider to decide if they claim them).
  - `LinkedIssuesProvider` — resolve any `#N` references to the issue
    title+body in the same repo. External ticket systems are handled by their
    own provider modules.

The regex set stays deliberately conservative to avoid false positives
(`HTTP-2` ≠ a Jira key). Callers always inspect `PRRefs.*` lists, so extra
nothing extra leaks.
"""

from __future__ import annotations

import logging
import re

from services.context.providers.base import ContextProvider, PRRefs
from services.engines.base import ContextChunk
from services.github_app import gh_client

log = logging.getLogger(__name__)


# Jira keys: 2-10 uppercase letters + dash + digits. Anchored to word boundary.
_JIRA_KEY = re.compile(r"\b([A-Z][A-Z0-9]{1,9})-(\d+)\b")
_GITHUB_ISSUE_SAME_REPO = re.compile(r"(?<!/)#(\d{1,7})\b")
_GITHUB_ISSUE_CROSS = re.compile(r"\b([\w.-]+/[\w.-]+)#(\d{1,7})\b")
_URL = re.compile(r"https?://[^\s)<>\"]+")


def extract_refs(
    *, pr_url: str, repo_slug: str, title: str, body: str, branch_name: str
) -> PRRefs:
    haystack = "\n".join((title or "", body or "", branch_name or ""))
    jira_keys = [f"{m.group(1)}-{m.group(2)}" for m in _JIRA_KEY.finditer(haystack)]
    gh_issues = _extract_gh_issues(haystack, repo_slug)
    urls = sorted(set(_URL.findall(haystack)))
    return PRRefs(
        pr_url=pr_url,
        repo_slug=repo_slug,
        title=title or "",
        body=body or "",
        branch_name=branch_name or "",
        jira_keys=sorted(set(jira_keys)),
        github_issues=sorted(set(gh_issues)),
        urls=urls,
    )


def _extract_gh_issues(text: str, repo_slug: str) -> list[int]:
    nums: list[int] = []
    for m in _GITHUB_ISSUE_SAME_REPO.finditer(text):
        nums.append(int(m.group(1)))
    for m in _GITHUB_ISSUE_CROSS.finditer(text):
        if m.group(1).lower() == repo_slug.lower():
            nums.append(int(m.group(2)))
    return nums


class LinkedIssuesProvider:
    """Fetches titles + bodies of same-repo `#N` issues referenced by the PR."""

    name = "github_linked_issues"

    def is_enabled(self) -> bool:
        from services.config import settings

        s = settings()
        return bool(s.github_pat or (s.github_app_id and s.github_app_private_key_path))

    def fetch(self, refs: PRRefs) -> list[ContextChunk]:
        if not refs.github_issues:
            return []
        chunks: list[ContextChunk] = []
        with gh_client(refs.repo_slug) as c:
            for num in refs.github_issues[:5]:  # cap — PRs rarely reference >5 real issues
                try:
                    r = c.get(f"/repos/{refs.repo_slug}/issues/{num}")
                    if r.status_code == 404:
                        continue
                    r.raise_for_status()
                    issue = r.json()
                except Exception:
                    log.exception("failed to fetch issue %s#%d", refs.repo_slug, num)
                    continue
                title = issue.get("title") or ""
                body = issue.get("body") or ""
                chunks.append(
                    ContextChunk(
                        source=f"github_issue_{num}",
                        title=f"#{num} {title}",
                        body=f"{title}\n\n{body}",
                        trust_level="untrusted",
                    )
                )
        return chunks


_PROVIDER: ContextProvider = LinkedIssuesProvider()
