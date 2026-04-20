"""GitHub PR context provider — fetches metadata, diff, and changed files."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from services.github_app import gh_client, parse_pr_url


@dataclass
class PRMetadata:
    owner: str
    repo: str
    number: int
    head_sha: str
    title: str
    body: str


@dataclass
class PRBundle(PRMetadata):
    diff: str
    files: list[dict[str, Any]]


def fetch_pr_metadata(pr_url: str) -> PRMetadata:
    ref = parse_pr_url(pr_url)
    with gh_client(ref.slug) as c:
        pr = c.get(f"/repos/{ref.slug}/pulls/{ref.number}").raise_for_status().json()
    return PRMetadata(
        owner=ref.owner,
        repo=ref.repo,
        number=ref.number,
        head_sha=pr["head"]["sha"],
        title=pr.get("title") or "",
        body=pr.get("body") or "",
    )


def fetch_pr_bundle(pr_url: str) -> PRBundle:
    ref = parse_pr_url(pr_url)
    with gh_client(ref.slug) as c:
        pr = c.get(f"/repos/{ref.slug}/pulls/{ref.number}").raise_for_status().json()
        files = c.get(f"/repos/{ref.slug}/pulls/{ref.number}/files").raise_for_status().json()
        diff_resp = c.get(
            f"/repos/{ref.slug}/pulls/{ref.number}",
            headers={"Accept": "application/vnd.github.v3.diff"},
        )
        diff_resp.raise_for_status()

    return PRBundle(
        owner=ref.owner,
        repo=ref.repo,
        number=ref.number,
        head_sha=pr["head"]["sha"],
        title=pr.get("title") or "",
        body=pr.get("body") or "",
        diff=diff_resp.text,
        files=files,
    )
