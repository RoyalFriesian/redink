"""ReviewEngine interface — pluggable brain for a review session.

Engines implemented as separate modules (ollama, claude_code, copilot, anthropic_api, openai_api).
`get_engine(name)` returns a cached instance, raising for unknown names.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Callable

ProgressCallback = Callable[[str], None]


@dataclass
class Finding:
    path: str
    line: int
    body: str
    severity: str = "info"  # info | warn | error


@dataclass
class ContextChunk:
    source: str
    title: str
    body: str
    trust_level: str = "untrusted"  # trusted (repo code) | untrusted (PR body, tickets, docs)


@dataclass
class ClarificationQuestion:
    id: str
    text: str
    why_needed: str


@dataclass
class RoundQA:
    """A completed clarification round: what was asked + the author's reply."""

    round_no: int
    questions: list[ClarificationQuestion]
    answer_text: str  # flattened author reply (free-form or per-question)


@dataclass
class ReviewContext:
    pr_url: str
    head_sha: str
    title: str
    body: str
    diff: str
    files: list[dict[str, Any]]
    chunks: list[ContextChunk] = field(default_factory=list)
    # Prior Q&A rounds, oldest first. Empty on the first evaluate pass.
    rounds: list[RoundQA] = field(default_factory=list)


@dataclass
class ContextEvaluation:
    sufficient: bool
    questions: list[ClarificationQuestion]
    # Model's running understanding of the PR — what it now knows, what's still
    # unclear. Surfaces chain-of-thought so the next round can build on it
    # instead of re-asking the same question.
    reasoning: str = ""


class ReviewEngine(ABC):
    name: str

    @abstractmethod
    def evaluate_context(self, ctx: ReviewContext) -> ContextEvaluation:
        """Return whether we have enough info to review, and if not, what to ask the author."""

    @abstractmethod
    def review(
        self, ctx: ReviewContext, *, on_progress: ProgressCallback | None = None
    ) -> list[Finding]:
        """Produce the list of review comments.

        `on_progress(text)` lets the engine narrate per-file progress into the
        caller's Slack thread. Optional — implementations may ignore it.
        """

    @abstractmethod
    def engage_on_reply(
        self, finding: Finding, reply_text: str, ctx: ReviewContext
    ) -> tuple[str, str]:
        """Given a human reply to one of our comments, return (action, response_body).

        action in {"concede", "clarify", "defend", "escalate"}.
        """


@lru_cache
def get_engine(name: str, model: str | None = None) -> ReviewEngine:
    """Return a cached engine instance bound to `(name, model)`.

    `model=None` means "use the engine's own default from settings". Passing
    an explicit model lets a single review session pin (e.g.) gemma3:12b or
    claude-opus-4-5 without mutating global config. Instances are cached per
    `(name, model)` tuple so repeat calls in-process share one client.
    """
    match name:
        case "ollama":
            from services.engines.ollama import OllamaEngine

            return OllamaEngine(model=model) if model else OllamaEngine()
        case "claude-code":
            from services.engines.claude_code import ClaudeCodeEngine

            return ClaudeCodeEngine(model=model) if model else ClaudeCodeEngine()
        case _:
            raise ValueError(f"unknown engine: {name!r}")


def resolve_model(engine: str, model: str | None) -> str:
    """Return the effective model name that `engine` will actually use.

    Used for surfacing the chosen model in Slack / CLI output. Keeps "what
    the user asked for" separate from "what the engine defaults to" so we
    never lie to the reviewer about which model produced a given comment.
    """
    from services.config import settings

    if model:
        return model
    s = settings()
    if engine == "ollama":
        return s.ollama_model
    if engine == "claude-code":
        return s.claude_code_model
    return "unknown"
