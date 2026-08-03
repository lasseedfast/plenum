"""Pydantic models for the deep-research engine.

Conventions mirror backend/services/research_models.py (the chat pre-pass),
but these describe *persistent* board state, not a request-scoped report.
"""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class ResearchFinding(BaseModel):
    """One concrete, grounded claim found during a research trip."""

    label: str = Field(..., description="Short concrete headline of the claim itself")
    detail: str = Field(default="", description="What the claim shows — no conclusions")
    quote: str = Field(default="", description="Short verbatim quote that grounds the claim")
    source_id: str = Field(default="", description="Bare talk id the quote comes from (e.g. 'H40911')")
    # Enriched deterministically after the trip from the seen-map — never trusted from the LLM.
    speaker: Optional[str] = None
    party: Optional[str] = None
    date: Optional[str] = None


class ResearchLead(BaseModel):
    """A next step worth taking: a new search, a person, or a debate to read."""

    kind: Literal["search", "person", "debate"]
    target: str = Field(..., description="Search query, person_id, or debate id")
    lead: str = Field(default="", description="What to do and why, in plain Swedish")
    # Display name for person/debate targets, resolved from the seen-map.
    label: Optional[str] = None


class ThreadResearch(BaseModel):
    """Distilled result of one bounded research trip."""

    findings: List[ResearchFinding] = Field(default_factory=list)
    open_questions: List[str] = Field(default_factory=list)
    leads: List[ResearchLead] = Field(default_factory=list)


class ThreadSeed(BaseModel):
    """One open thread proposed by the discovery pass."""

    title: str
    question: str
    why: str = ""
    hints: List[str] = Field(default_factory=list)


class BoardSeeds(BaseModel):
    # Required on purpose: with a default it drops out of the JSON schema's
    # "required" list and the model reliably omits it.
    title: str = Field(
        ...,
        description=("Descriptive headline for the whole exploration: 3-8 ordinary Swedish "
                     "words, max 60 characters, no coined compounds, no trailing year"),
    )
    intro: str = ""
    threads: List[ThreadSeed] = Field(default_factory=list)


class ScoutQueries(BaseModel):
    """Follow-up search queries proposed between scout rounds."""

    queries: List[str] = Field(default_factory=list)
