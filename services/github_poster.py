"""Post a batched review (inline comments + summary) back to GitHub.

M1 posts a single `COMMENT` review with N inline comments pinned to file+line.
M3 extends this with `post_comment_reply(comment_id, body)` for the engagement loop.
"""

from __future__ import annotations

import logging

from services.engines.base import Finding, ReviewContext
from services.github_app import gh_client, parse_pr_url
from services.review.secret_scrubber import scrub_outbound

log = logging.getLogger(__name__)


def post_review(
    ctx: ReviewContext,
    findings: list[Finding],
    finding_rows: list,  # ORM rows; we stamp posted_comment_id back onto these
    *,
    summary_caveat: str | None = None,
) -> str | None:
    """Post a review to the PR. Returns the review's HTML url, or None if nothing to post."""
    if not findings:
        log.info("no findings to post for %s", ctx.pr_url)
        return None

    ref = parse_pr_url(ctx.pr_url)
    # Scrub every outbound body: a hallucinated reviewer comment must never
    # re-broadcast a secret that appeared anywhere upstream (diff, ticket, doc).
    comments = [
        {
            "path": f.path,
            "line": f.line,
            "side": "RIGHT",
            "body": scrub_outbound(f.body),
        }
        for f in findings
    ]

    payload = {
        "commit_id": ctx.head_sha,
        "body": scrub_outbound(_summary_body(findings, caveat=summary_caveat)),
        "event": "COMMENT",
        "comments": comments,
    }

    with gh_client(ref.slug) as c:
        resp = c.post(
            f"/repos/{ref.slug}/pulls/{ref.number}/reviews", json=payload
        )
        resp.raise_for_status()
        review = resp.json()

        # Fetch the review comments so we can map posted_comment_id back to our findings.
        posted = c.get(
            f"/repos/{ref.slug}/pulls/{ref.number}/reviews/{review['id']}/comments"
        ).raise_for_status().json()

    for row, posted_c in zip(finding_rows, posted, strict=False):
        row.posted_comment_id = posted_c.get("id")

    return review.get("html_url")


def post_comment_reply(repo_slug: str, pr_number: int, parent_comment_id: int, body: str) -> int:
    """Reply in-thread to an existing review comment (M3 engagement loop)."""
    with gh_client(repo_slug) as c:
        resp = c.post(
            f"/repos/{repo_slug}/pulls/{pr_number}/comments/{parent_comment_id}/replies",
            json={"body": scrub_outbound(body)},
        )
        resp.raise_for_status()
        return resp.json()["id"]


def _summary_body(findings: list[Finding], *, caveat: str | None = None) -> str:
    n = len(findings)
    plural = "finding" if n == 1 else "findings"
    severities: dict[str, int] = {}
    for f in findings:
        severities[f.severity] = severities.get(f.severity, 0) + 1
    breakdown = ", ".join(f"{v} {k}" for k, v in severities.items())
    lines = [
        f"Redink reviewed this PR and left **{n} {plural}** ({breakdown}).",
        "",
        "Reply directly to any inline comment to discuss — Redink will engage back "
        "up to 3 rounds before flagging for a human.",
    ]
    if caveat:
        lines.insert(0, caveat)
        lines.insert(1, "")
    return "\n".join(lines)
