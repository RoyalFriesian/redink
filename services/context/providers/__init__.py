"""Provider registry.

`all_providers()` returns the set of providers we try on every review. Each
returns [] when its creds aren't configured, so this list is safe to run even
on a fresh install with only GitHub set up.
"""

from __future__ import annotations

from services.context.providers.base import ContextProvider
from services.context.providers.confluence import ConfluenceProvider
from services.context.providers.jira import JiraProvider
from services.context.providers.linked_issues import LinkedIssuesProvider
from services.context.providers.repo_snapshot import RepoSnapshotProvider


def all_providers() -> list[ContextProvider]:
    # RepoSnapshotProvider runs first so repo-level context is in place before
    # ticket/design-doc providers layer their ticket-specific details on top.
    return [
        RepoSnapshotProvider(),
        LinkedIssuesProvider(),
        JiraProvider(),
        ConfluenceProvider(),
    ]
