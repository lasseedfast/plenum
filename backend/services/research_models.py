"""Pydantic models for the orchestrator/researcher protocol."""

from typing import List, Literal

from pydantic import BaseModel, Field


class SubQuestion(BaseModel):
    id: str
    question: str
    needs_quotes: bool = False
    hints: List[str] = Field(default_factory=list)


class ResearchRequest(BaseModel):
    user_message: str
    sub_questions: List[SubQuestion]
    notes: str = ""


class SubFinding(BaseModel):
    sub_question_id: str
    answer: str
    source_ids: List[str] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low"] = "medium"
    gaps: str = ""


class ResearchReport(BaseModel):
    findings: List[SubFinding]
    dead_ends: List[str] = Field(default_factory=list)
    overall_notes: str = ""
