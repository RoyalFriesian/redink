"""Detailed repo introspection provider.

Goal: give the reviewer enough information to answer "what is this repo
actually about?" before it has to ask the PR author. Collects, in a single
provider run:

  1. **Repo metadata** — description, topics, primary language, default
     branch. Cheap, one API call.
  2. **Root design docs** — README, ARCHITECTURE, DESIGN, CONTRIBUTING,
     SECURITY, CODEOWNERS. If present, these define the repo's purpose.
  3. **Service/package READMEs** — every README.md between a changed file
     and the repo root, so multi-service monorepos get the right context
     per PR.
  4. **Top-level tree** — the first two levels of the repo layout, so the
     reviewer can reason about sibling services and shared libraries.
  5. **Package manifests** — go.mod / package.json / pyproject.toml /
     Cargo.toml / pom.xml / build.gradle at the root and in each changed
     directory (just the first ~5KB, enough for name/description/deps).
  6. **Full changed-file bodies** — every file in the PR, fetched at
     head_sha, up to `REDINK_SNAPSHOT_FILE_BYTES` per file. The diff alone
     often lacks the surrounding code the reviewer needs.

**Memory caching.** Repo-wide items (1-5) are cached in the memory layer
keyed on `repo_snapshot:<slug>`, with the **default branch commit SHA** as
the etag. Before every reuse we fetch the current default-branch SHA and
compare. Mismatch → refetch. This is the "always check if memory is updated
before using it" contract. Per-file bodies (6) are keyed on
`repo_file:<slug>:<path>@<head_sha>` so they're intrinsically pinned and
never go stale.

**Trust level.** Repo files are marked `trusted` — they're governed by the
same CODEOWNERS as the code under review, so they're no more hostile than
the diff itself.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from typing import Any

from services.context.providers.base import PRRefs
from services.engines.base import ContextChunk
from services.github_app import gh_client
from services.memory import get_memory

log = logging.getLogger(__name__)

_ROOT_DESIGN_DOCS = (
    "README.md",
    "README.rst",
    "ARCHITECTURE.md",
    "DESIGN.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "CODEOWNERS",
    ".github/CODEOWNERS",
    "docs/README.md",
    "docs/architecture.md",
)
_PACKAGE_MANIFESTS = (
    "go.mod",
    "package.json",
    "pyproject.toml",
    "Cargo.toml",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "requirements.txt",
    "Gemfile",
)
_MANIFEST_HEAD_BYTES = 5_000
_TREE_MAX_ENTRIES = 200


@dataclass
class _SnapshotCacheValue:
    """Serialisable shape of what lives in the memory cache."""

    repo_meta: dict[str, Any]
    root_tree: list[str]
    root_docs: dict[str, str]  # path → body
    root_manifests: dict[str, str]
    service_docs: dict[str, str]
    service_manifests: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_meta": self.repo_meta,
            "root_tree": self.root_tree,
            "root_docs": self.root_docs,
            "root_manifests": self.root_manifests,
            "service_docs": self.service_docs,
            "service_manifests": self.service_manifests,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> _SnapshotCacheValue:
        return cls(
            repo_meta=d.get("repo_meta") or {},
            root_tree=d.get("root_tree") or [],
            root_docs=d.get("root_docs") or {},
            root_manifests=d.get("root_manifests") or {},
            service_docs=d.get("service_docs") or {},
            service_manifests=d.get("service_manifests") or {},
        )


class RepoSnapshotProvider:
    name = "repo_snapshot"

    def is_enabled(self) -> bool:
        from services.config import settings

        s = settings()
        return bool(s.github_pat or (s.github_app_id and s.github_app_private_key_path))

    def fetch(self, refs: PRRefs) -> list[ContextChunk]:
        from services.config import settings

        s = settings()
        mem = get_memory() if s.redink_memory_enabled else None

        with gh_client(refs.repo_slug) as c:
            changed_paths = self._changed_paths(c, refs)
            changed_dirs = sorted({_parent_dir(p) for p in changed_paths})

            # Repo-wide cached slice ---------------------------------------
            default_branch_sha = self._default_branch_sha(c, refs.repo_slug)
            snapshot = self._load_snapshot(
                mem=mem,
                repo_slug=refs.repo_slug,
                default_branch_sha=default_branch_sha,
                client=c,
                changed_dirs=changed_dirs,
            )

            # Per-file bodies at head_sha (pinned, cacheable forever) ------
            head_sha = self._pr_head_sha(c, refs)
            file_chunks = self._fetch_changed_file_bodies(
                client=c,
                repo_slug=refs.repo_slug,
                paths=changed_paths,
                head_sha=head_sha,
                mem=mem,
                file_byte_cap=s.redink_snapshot_file_bytes,
                total_byte_cap=s.redink_snapshot_total_bytes,
            )

        return self._render_chunks(snapshot) + file_chunks

    # ---------------- repo-wide snapshot ---------------------------------

    def _load_snapshot(
        self,
        *,
        mem,
        repo_slug: str,
        default_branch_sha: str,
        client,
        changed_dirs: list[str],
    ) -> _SnapshotCacheValue:
        key = f"repo_snapshot:{repo_slug}"
        if mem is not None:
            hit = mem.get(key, expected_etag=default_branch_sha)
            if hit is not None:
                log.info(
                    "repo_snapshot: memory hit for %s @ %s (captured %s)",
                    repo_slug,
                    default_branch_sha[:7],
                    hit.captured_at,
                )
                cached = _SnapshotCacheValue.from_dict(hit.value)
                # Top up with any service-level docs/manifests for dirs we
                # haven't seen before (different PR, different dirs).
                missing_dirs = [d for d in changed_dirs if d not in cached.service_docs and d]
                if missing_dirs:
                    extra_docs, extra_manifests = self._fetch_service_docs_and_manifests(
                        client, repo_slug, missing_dirs
                    )
                    cached.service_docs.update(extra_docs)
                    cached.service_manifests.update(extra_manifests)
                    mem.put(key, cached.to_dict(), etag=default_branch_sha)
                return cached

        # Miss — fetch everything fresh.
        log.info("repo_snapshot: cold fetch for %s @ %s", repo_slug, default_branch_sha[:7])
        repo_meta = self._fetch_repo_meta(client, repo_slug)
        root_tree = self._fetch_root_tree(client, repo_slug, default_branch_sha)
        root_docs = self._fetch_paths(client, repo_slug, list(_ROOT_DESIGN_DOCS))
        root_manifests = self._fetch_paths(
            client, repo_slug, list(_PACKAGE_MANIFESTS), head_bytes=_MANIFEST_HEAD_BYTES
        )
        service_docs, service_manifests = self._fetch_service_docs_and_manifests(
            client, repo_slug, changed_dirs
        )

        snap = _SnapshotCacheValue(
            repo_meta=repo_meta,
            root_tree=root_tree,
            root_docs=root_docs,
            root_manifests=root_manifests,
            service_docs=service_docs,
            service_manifests=service_manifests,
        )
        if mem is not None:
            mem.put(key, snap.to_dict(), etag=default_branch_sha)
        return snap

    def _render_chunks(self, snap: _SnapshotCacheValue) -> list[ContextChunk]:
        chunks: list[ContextChunk] = []

        meta = snap.repo_meta
        if meta:
            meta_body = "\n".join(
                f"{k}: {v}"
                for k, v in [
                    ("full_name", meta.get("full_name")),
                    ("description", meta.get("description")),
                    ("topics", ", ".join(meta.get("topics") or [])),
                    ("primary_language", meta.get("language")),
                    ("default_branch", meta.get("default_branch")),
                    ("size_kb", meta.get("size")),
                    ("homepage", meta.get("homepage")),
                ]
                if v
            )
            if meta_body:
                chunks.append(
                    ContextChunk(
                        source="repo_meta",
                        title=f"Repo metadata — {meta.get('full_name', '')}",
                        body=meta_body,
                        trust_level="trusted",
                    )
                )

        if snap.root_tree:
            tree_body = "\n".join(snap.root_tree[:_TREE_MAX_ENTRIES])
            chunks.append(
                ContextChunk(
                    source="repo_tree",
                    title="Repo top-level layout",
                    body=tree_body,
                    trust_level="trusted",
                )
            )

        for path, body in sorted(snap.root_docs.items()):
            chunks.append(
                ContextChunk(
                    source=f"repo_doc:{path}",
                    title=path,
                    body=body,
                    trust_level="trusted",
                )
            )

        for path, body in sorted(snap.root_manifests.items()):
            chunks.append(
                ContextChunk(
                    source=f"repo_manifest:{path}",
                    title=f"{path} (head)",
                    body=body,
                    trust_level="trusted",
                )
            )

        for path, body in sorted(snap.service_docs.items()):
            chunks.append(
                ContextChunk(
                    source=f"service_doc:{path}",
                    title=path,
                    body=body,
                    trust_level="trusted",
                )
            )

        for path, body in sorted(snap.service_manifests.items()):
            chunks.append(
                ContextChunk(
                    source=f"service_manifest:{path}",
                    title=f"{path} (head)",
                    body=body,
                    trust_level="trusted",
                )
            )

        return chunks

    # ---------------- per-file bodies ------------------------------------

    def _fetch_changed_file_bodies(
        self,
        *,
        client,
        repo_slug: str,
        paths: list[str],
        head_sha: str,
        mem,
        file_byte_cap: int,
        total_byte_cap: int,
    ) -> list[ContextChunk]:
        out: list[ContextChunk] = []
        running_total = 0
        for path in paths:
            if running_total >= total_byte_cap:
                log.info("repo_snapshot: per-review total-bytes cap hit, stopping file fetches")
                break
            body = self._fetch_file_at(
                client=client,
                repo_slug=repo_slug,
                path=path,
                sha=head_sha,
                mem=mem,
                max_bytes=file_byte_cap,
            )
            if body is None:
                continue
            running_total += len(body)
            out.append(
                ContextChunk(
                    source=f"repo_file:{path}@{head_sha[:7]}",
                    title=f"{path} (full file at {head_sha[:7]})",
                    body=body,
                    trust_level="trusted",
                )
            )
        return out

    def _fetch_file_at(
        self,
        *,
        client,
        repo_slug: str,
        path: str,
        sha: str,
        mem,
        max_bytes: int,
    ) -> str | None:
        # SHA-pinned file content is immutable — cache forever, no staleness check needed.
        key = f"repo_file:{repo_slug}:{path}@{sha}"
        if mem is not None:
            hit = mem.get(key, expected_etag=sha)
            if hit is not None:
                return hit.value.get("body")

        body = _fetch_path_body(client, repo_slug, path, ref=sha, head_bytes=max_bytes)
        if body is None:
            return None
        if mem is not None:
            mem.put(key, {"body": body}, etag=sha)
        return body

    # ---------------- small helpers --------------------------------------

    def _changed_paths(self, client, refs: PRRefs) -> list[str]:
        pr_number = refs.pr_url.rstrip("/").rsplit("/", 1)[-1]
        try:
            r = client.get(f"/repos/{refs.repo_slug}/pulls/{pr_number}/files")
            r.raise_for_status()
            return [f.get("filename") for f in r.json() if f.get("filename")]
        except Exception:
            log.exception("failed to list PR files for repo_snapshot")
            return []

    def _pr_head_sha(self, client, refs: PRRefs) -> str:
        pr_number = refs.pr_url.rstrip("/").rsplit("/", 1)[-1]
        try:
            r = client.get(f"/repos/{refs.repo_slug}/pulls/{pr_number}")
            r.raise_for_status()
            return r.json()["head"]["sha"]
        except Exception:
            log.exception("failed to get PR head sha for repo_snapshot")
            return ""

    def _default_branch_sha(self, client, repo_slug: str) -> str:
        try:
            repo = client.get(f"/repos/{repo_slug}").raise_for_status().json()
            branch_name = repo.get("default_branch") or "main"
            br = client.get(f"/repos/{repo_slug}/branches/{branch_name}").raise_for_status().json()
            return br.get("commit", {}).get("sha", "") or ""
        except Exception:
            log.exception("failed to resolve default branch SHA for %s", repo_slug)
            return "unknown"

    def _fetch_repo_meta(self, client, repo_slug: str) -> dict[str, Any]:
        try:
            r = client.get(f"/repos/{repo_slug}")
            r.raise_for_status()
            d = r.json()
            return {
                "full_name": d.get("full_name"),
                "description": d.get("description"),
                "topics": d.get("topics") or [],
                "language": d.get("language"),
                "default_branch": d.get("default_branch"),
                "size": d.get("size"),
                "homepage": d.get("homepage"),
            }
        except Exception:
            log.exception("failed to fetch repo meta")
            return {}

    def _fetch_root_tree(self, client, repo_slug: str, sha: str) -> list[str]:
        try:
            r = client.get(f"/repos/{repo_slug}/git/trees/{sha}")
            r.raise_for_status()
            tree = r.json().get("tree") or []
        except Exception:
            log.exception("failed to fetch root tree")
            return []
        entries = []
        for t in tree:
            path = t.get("path")
            kind = "dir/" if t.get("type") == "tree" else ""
            if path:
                entries.append(f"{kind}{path}")
        return sorted(entries)

    def _fetch_paths(
        self,
        client,
        repo_slug: str,
        candidates: list[str],
        *,
        head_bytes: int | None = None,
    ) -> dict[str, str]:
        out: dict[str, str] = {}
        for p in candidates:
            body = _fetch_path_body(client, repo_slug, p, head_bytes=head_bytes)
            if body:
                out[p] = body
        return out

    def _fetch_service_docs_and_manifests(
        self,
        client,
        repo_slug: str,
        changed_dirs: list[str],
    ) -> tuple[dict[str, str], dict[str, str]]:
        """For each changed dir (and each parent up to root), pull its README +
        any package manifest sitting there. Deduped across dirs.
        """
        docs: dict[str, str] = {}
        manifests: dict[str, str] = {}

        seen_dirs: set[str] = set()
        for d in changed_dirs:
            parts = d.split("/") if d else []
            for i in range(len(parts), 0, -1):  # walk up from the file's dir to root
                sub = "/".join(parts[:i])
                if sub in seen_dirs or not sub:
                    continue
                seen_dirs.add(sub)
                for doc_name in ("README.md", "README.rst"):
                    path = f"{sub}/{doc_name}"
                    if path in docs:
                        continue
                    body = _fetch_path_body(client, repo_slug, path)
                    if body:
                        docs[path] = body
                        break
                for manifest in _PACKAGE_MANIFESTS:
                    path = f"{sub}/{manifest}"
                    if path in manifests:
                        continue
                    body = _fetch_path_body(
                        client, repo_slug, path, head_bytes=_MANIFEST_HEAD_BYTES
                    )
                    if body:
                        manifests[path] = body
        return docs, manifests


def _fetch_path_body(
    client,
    repo_slug: str,
    path: str,
    *,
    ref: str | None = None,
    head_bytes: int | None = None,
) -> str | None:
    """Fetch a file's contents from the GitHub contents API. Returns None on
    404 or any decoding failure. Truncates to `head_bytes` if provided.
    """
    try:
        params = {"ref": ref} if ref else None
        r = client.get(f"/repos/{repo_slug}/contents/{path}", params=params)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        data = r.json()
    except Exception:
        log.debug("path fetch miss: %s/%s", repo_slug, path, exc_info=True)
        return None

    if isinstance(data, list):  # directory — not a file
        return None
    if data.get("encoding") != "base64":
        return None
    try:
        raw = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    except Exception:
        return None
    if head_bytes is not None and len(raw) > head_bytes:
        raw = raw[:head_bytes] + "\n…(truncated)"
    return raw


def _parent_dir(path: str) -> str:
    if "/" not in path:
        return ""
    return path.rsplit("/", 1)[0]
