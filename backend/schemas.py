from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


class SearchFilters(BaseModel):
    parties: List[str] = Field(default_factory=list)
    people: List[str] = Field(default_factory=list)
    debates: List[str] = Field(default_factory=list)
    from_year: Optional[int] = None
    to_year: Optional[int] = None


class SearchRequest(SearchFilters):
    q: str = ""
    limit: int = None
    include_snippets: bool = True
    speaker: Optional[str] = None  # Exact speaker match if user selects it.
    speaker_ids: Optional[List[str]] = None  # The _key from the people collection
    
    class Config:
        # This ensures None values are included in the serialized output
        # and helps with debugging
        json_schema_extra = {
            "example": {
                "q": "ekonomi",
                "speaker": "Anders Borg",
                "speaker_ids": ["people/12345"],
                "limit": 100
            }
        }


class TalkHit(BaseModel):
    id: str = Field(..., alias="_id")  # Use 'id' as field name, alias to '_id'
    text: str
    snippet: Optional[str] = None  # Add default to make validation more forgiving
    snippet_long: Optional[str] = None
    number: Optional[int] = None
    debate_type: Optional[str] = None
    speaker: Optional[str] = None
    date: Optional[str] = None
    year: Optional[int] = None
    url_session: Optional[str] = None
    party: Optional[str] = None
    url_audio: Optional[str] = None
    audio_start_seconds: Optional[int] = None
    intressent_id: Optional[str] = None
    
    class Config:
        # Allow extra fields from the database that we don't explicitly define
        extra = "ignore"
        validate_by_name = True
        allow_population_by_alias = True


class AggregatedStats(BaseModel):
    per_party: dict[str, int]
    per_year: dict[int, int]
    total: int


class SearchResponse(BaseModel):
    results: List[TalkHit]
    stats: AggregatedStats
    active_filters: SearchFilters
    limit_reached: bool = False
    generated_at: datetime = Field(default_factory=datetime.utcnow)


class FeedbackRequest(BaseModel):
    message: str


class FeedbackResponse(BaseModel):
    status: str = "ok"


class ChatTurn(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: List[ChatTurn]
    limit: int = 5  # semantic context size


class ChatResponse(BaseModel):
    reply: str
    citations: List[TalkHit]
