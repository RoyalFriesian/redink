"""GitHub auth helper — supports two modes, in order of precedence:

1. **Personal access token** (`GITHUB_PAT`) — POC / single-user path. No App
   install, no webhook signing; comments are attributed to the PAT owner.
2. **GitHub App** (`GITHUB_APP_ID` + `GITHUB_APP_PRIVATE_KEY_PATH`) — production
   path: JWT → installation token, cached until expiry; bot attribution;
   required for the M3 webhook-driven engagement loop.

Both modes share the same `gh_client(repo_slug)` surface so context fetchers
and posters never have to know which is active.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import jwt

from services.config import settings

log = logging.getLogger(__name__)


_pr_url_re = re.compile(
    r"^https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)/?"
)


@dataclass
class PRRef:
    owner: str
    repo: str
    number: int

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"


def parse_pr_url(url: str) -> PRRef:
    m = _pr_url_re.match(url.strip())
    if not m:
        raise ValueError(f"not a GitHub PR URL: {url!r}")
    return PRRef(m["owner"], m["repo"], int(m["number"]))


_token_cache: dict[str, tuple[str, float]] = {}


def installation_token(repo_slug: str) -> str:
    """Return a GitHub App installation token for the given repo, caching until expiry."""
    now = time.time()
    cached = _token_cache.get(repo_slug)
    if cached and cached[1] - 60 > now:
        return cached[0]

    app_id = settings().github_app_id
    key_path = settings().github_app_private_key_path
    if not app_id or not key_path:
        raise RuntimeError(
            "No GitHub credentials configured. Set GITHUB_PAT for POC usage, or "
            "GITHUB_APP_ID + GITHUB_APP_PRIVATE_KEY_PATH for the App flow."
        )

    private_key = Path(key_path).read_text()
    jwt_token = jwt.encode(
        {"iat": int(now) - 30, "exp": int(now) + 540, "iss": app_id},
        private_key,
        algorithm="RS256",
    )
    headers = {"Authorization": f"Bearer {jwt_token}", "Accept": "application/vnd.github+json"}

    with httpx.Client(base_url="https://api.github.com", headers=headers, timeout=20) as c:
        inst = c.get(f"/repos/{repo_slug}/installation").raise_for_status().json()
        resp = c.post(f"/app/installations/{inst['id']}/access_tokens").raise_for_status().json()

    token, exp_iso = resp["token"], resp["expires_at"]
    # expires_at is ISO8601; convert to epoch for the cache.
    from datetime import datetime

    exp_epoch = datetime.fromisoformat(exp_iso.replace("Z", "+00:00")).timestamp()
    _token_cache[repo_slug] = (token, exp_epoch)
    return token


def _auth_token(repo_slug: str) -> str:
    """Return a token usable in `Authorization: token <...>`.

    PAT wins when set — the POC path avoids the App-install flow entirely.
    """
    pat = settings().github_pat
    if pat:
        return pat
    return installation_token(repo_slug)


def gh_client(repo_slug: str) -> httpx.Client:
    return httpx.Client(
        base_url="https://api.github.com",
        headers={
            "Authorization": f"token {_auth_token(repo_slug)}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=30,
    )
