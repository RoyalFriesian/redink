"""Pydantic schemas for engine outputs.

Small local models (Gemma `e2b`) produce unreliable free-form JSON. Everything the
engine returns is validated against these schemas; invalid output triggers a stricter
reprompt (`OllamaEngine._generate_json_with_retry`). If all retries fail we fall back
to a "couldn't review confidently" comment rather than hallucinating findings.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

Severity = Literal["info", "warn", "error"]


class ClarificationQuestionOut(BaseModel):
    id: str = Field(..., min_length=1, max_length=40)
    text: str = Field(..., min_length=5, max_length=500)
    why_needed: str = Field(..., min_length=5, max_length=500)


class ContextEvaluationOut(BaseModel):
    # Field order matters: the grammar-constrained decoder emits JSON keys in
    # declaration order. Putting `reasoning` first forces the model to write a
    # short chain-of-thought BEFORE committing to `sufficient`/`questions`,
    # which is the lightweight "recursive thinking" nudge for a 5B model.
    # Required (no default) so grammar-constrained decode forces the model to
    # emit it — a default would let pydantic mark it optional and gemma4:e2b
    # would skip the field entirely (observed: `reasoning: (none)` in logs).
    reasoning: str = Field(..., min_length=1, max_length=1500)
    sufficient: bool
    questions: list[ClarificationQuestionOut] = Field(default_factory=list)

    @field_validator("questions")
    @classmethod
    def _cap_questions(cls, v: list) -> list:
        # Never ask more than 3 at a time — prevents Gemma from vomiting a wall of Qs.
        return v[:3]


class FindingOut(BaseModel):
    path: str = Field(..., min_length=1, max_length=512)
    line: int = Field(..., ge=1)
    severity: Severity = "info"
    body: str = Field(..., min_length=5, max_length=2000)


class FileReviewOut(BaseModel):
    # `path` has a default because the caller already knows it from the PR
    # diff — we don't need the model to echo it back. Letting this be optional
    # also lets `_unwrap` handle the "model used the filename as the top-level
    # JSON key" shape without forcing a retry.
    path: str = ""
    findings: list[FindingOut] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _unwrap(cls, data):
        """Accept both the canonical shape and gemma's occasional nested shape.

        Without grammar-constrained decode, gemma4:e2b sometimes emits
        `{"<file/path>": {"findings": [...]}}` instead of
        `{"path": "<file/path>", "findings": [...]}`. We detect that
        (single top-level key, dict value, no `findings` at top level) and
        unwrap it — so a retry isn't burned just to re-shape the same content.
        """
        if not isinstance(data, dict):
            return data
        if "findings" in data or "path" in data:
            return data
        if len(data) == 1:
            (key, inner), = data.items()
            if isinstance(inner, dict):
                out = dict(inner)
                out.setdefault("path", key)
                return out
        return data

    @field_validator("findings")
    @classmethod
    def _cap_findings(cls, v: list) -> list:
        # Keep per-file output bounded for small models.
        return v[:8]


class EngageActionOut(BaseModel):
    action: Literal["concede", "clarify", "defend", "escalate"]
    body: str = Field(..., min_length=5, max_length=1500)
