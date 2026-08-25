"""Pydantic models for the orchestrator/researcher protocol."""

from typing import Literal

from pydantic import BaseModel, Field


class SubQuestion(BaseModel):
    id: str
    question: str
    needs_quotes: bool = False
    hints: list[str] = Field(default_factory=list)


class ResearchRequest(BaseModel):
    user_message: str
    sub_questions: list[SubQuestion]
    notes: str = ""


class SubFinding(BaseModel):
    sub_question_id: str
    answer: str
    source_ids: list[str] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low"] = "medium"
    gaps: str = ""


class ResearchReport(BaseModel):
    findings: list[SubFinding]
    dead_ends: list[str] = Field(default_factory=list)
    overall_notes: str = ""
