"""Jira ticket provider — deep fetch for review context.

For each key referenced by the PR we pull description, recent comments,
subtasks, and the parent epic (depth-1 recursion). Every emitted
`ContextChunk` is tagged `jira:<KEY>` / `jira:<KEY>:comments` /
`jira:<KEY>:subtask:<CHILD>` / `jira:<KEY>:parent:<EPIC>` so the reviewer
prompt (and the eval harness) can see exactly which slice of Jira fed which
piece of reasoning.

Auth: Cloud Basic (`email + API token`). On-prem PAT is the same shape.
Connectivity is optional — if `JIRA_BASE_URL` / Atlassian creds aren't
configured the provider reports disabled and is silently skipped.

ADF (Atlassian Document Format) descriptions are flattened by `_flatten_adf`;
anything exotic (tables, media) renders as a `[type]` placeholder rather than
breaking the prompt.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from services.context.providers.base import PRRefs
from services.engines.base import ContextChunk

log = logging.getLogger(__name__)

# Caps chosen to protect token budget + Jira rate limits. A single PR rarely
# references more than a couple of tickets; deep recursion past depth 1
# (subtask-of-subtask, grandparent epic) hasn't paid off in our eval set.
_MAX_TOP_LEVEL = 10
_MAX_SUBTASKS = 5
_MAX_COMMENTS = 10
_TICKET_FIELDS = (
    "summary,description,status,issuetype,priority,labels,"
    "assignee,reporter,created,updated,comment,subtasks,parent"
)


class JiraProvider:
    name = "jira"

    def is_enabled(self) -> bool:
        from services.config import settings

        s = settings()
        return bool(s.jira_base_url and s.jira_auth())

    def fetch(self, refs: PRRefs) -> list[ContextChunk]:
        if not refs.jira_keys:
            return []
        from services.config import settings

        s = settings()
        base = s.jira_base_url.rstrip("/")
        auth = s.jira_auth()
        if auth is None:
            return []

        chunks: list[ContextChunk] = []
        # Track keys we've already fetched so a parent epic shared across
        # sibling tickets (or a subtask that was also listed explicitly on the
        # PR) doesn't get pulled twice.
        seen: set[str] = set()

        with httpx.Client(timeout=30.0) as c:
            for key in refs.jira_keys[:_MAX_TOP_LEVEL]:
                self._fetch_and_emit(
                    c, base, auth, key, chunks, seen, depth=0,
                )
        return chunks

    def _fetch_and_emit(
        self,
        client: httpx.Client,
        base: str,
        auth: tuple[str, str],
        key: str,
        out: list[ContextChunk],
        seen: set[str],
        *,
        depth: int,
        parent_of: str | None = None,
        subtask_of: str | None = None,
    ) -> None:
        if key in seen:
            return
        seen.add(key)
        issue = self._get_issue(client, base, auth, key)
        if issue is None:
            return

        fields = issue.get("fields") or {}
        summary = fields.get("summary") or ""
        status = (fields.get("status") or {}).get("name") or ""
        itype = (fields.get("issuetype") or {}).get("name") or ""
        prio = (fields.get("priority") or {}).get("name") or ""
        labels = ", ".join(fields.get("labels") or [])
        assignee = ((fields.get("assignee") or {}).get("displayName")) or "unassigned"
        reporter = ((fields.get("reporter") or {}).get("displayName")) or "unknown"
        desc = _flatten_adf(fields.get("description"))

        header = f"{itype} · {status} · priority {prio or 'n/a'}"
        meta = f"assignee: {assignee} · reporter: {reporter}"
        if labels:
            meta += f" · labels: {labels}"

        body = f"{header}\n{meta}\n\n{summary}\n\n{desc}".strip()
        source = f"jira:{key}"
        if subtask_of:
            source = f"jira:{subtask_of}:subtask:{key}"
        elif parent_of:
            source = f"jira:{parent_of}:parent:{key}"
        out.append(
            ContextChunk(
                source=source,
                title=f"{key} {summary}",
                body=body,
                trust_level="untrusted",
            )
        )

        comments_text = _format_comments(fields.get("comment"))
        if comments_text:
            out.append(
                ContextChunk(
                    source=f"jira:{key}:comments",
                    title=f"{key} comments",
                    body=comments_text,
                    trust_level="untrusted",
                )
            )

        # Depth 1 only — deeper recursion has produced noise without extra signal.
        if depth >= 1:
            return

        for sub in (fields.get("subtasks") or [])[:_MAX_SUBTASKS]:
            sub_key = sub.get("key")
            if not sub_key:
                continue
            self._fetch_and_emit(
                client, base, auth, sub_key, out, seen,
                depth=depth + 1, subtask_of=key,
            )

        parent = fields.get("parent") or {}
        parent_key = parent.get("key")
        if parent_key:
            self._fetch_and_emit(
                client, base, auth, parent_key, out, seen,
                depth=depth + 1, parent_of=key,
            )

    def _get_issue(
        self,
        client: httpx.Client,
        base: str,
        auth: tuple[str, str],
        key: str,
    ) -> dict | None:
        try:
            r = client.get(
                f"{base}/rest/api/3/issue/{key}",
                auth=auth,
                params={"fields": _TICKET_FIELDS, "expand": "renderedFields"},
            )
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        except Exception:
            log.exception("jira fetch failed for %s", key)
            return None


def _format_comments(comment_field: Any) -> str:
    """Turn the `fields.comment.comments` list into a linear thread."""
    if not isinstance(comment_field, dict):
        return ""
    comments = comment_field.get("comments") or []
    if not isinstance(comments, list) or not comments:
        return ""
    # Most recent last: Jira already returns oldest → newest.
    tail = comments[-_MAX_COMMENTS:]
    lines: list[str] = []
    for cm in tail:
        author = ((cm.get("author") or {}).get("displayName")) or "unknown"
        created = cm.get("created") or ""
        body = _flatten_adf(cm.get("body")).strip()
        if not body:
            continue
        lines.append(f"[{created}] {author}:\n{body}")
    return "\n\n".join(lines)


def _flatten_adf(node: Any) -> str:
    """Walk Atlassian Document Format and emit plain text.

    Unrecognised node types render as `[type]` so the reviewer can see something
    was elided. No attempt at faithful markdown reconstruction — we just need
    enough text for the LLM to understand intent.
    """
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_flatten_adf(n) for n in node)
    if not isinstance(node, dict):
        return ""

    node_type = node.get("type") or ""
    if node_type == "text":
        return node.get("text") or ""
    if node_type in {"hardBreak"}:
        return "\n"
    if node_type == "paragraph":
        return _flatten_adf(node.get("content")) + "\n"
    if node_type in {"bulletList", "orderedList"}:
        return _flatten_adf(node.get("content"))
    if node_type == "listItem":
        return "- " + _flatten_adf(node.get("content")).strip() + "\n"
    if node_type == "heading":
        return "\n" + _flatten_adf(node.get("content")).strip() + "\n"
    if node_type == "codeBlock":
        return "\n```\n" + _flatten_adf(node.get("content")) + "\n```\n"
    if node_type == "doc":
        return _flatten_adf(node.get("content"))
    if "content" in node:
        return _flatten_adf(node["content"])
    return f"[{node_type}]"
