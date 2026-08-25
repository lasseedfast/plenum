from datetime import datetime

from pydantic import BaseModel, Field


class SearchFilters(BaseModel):
    parties: list[str] = Field(default_factory=list)
    people: list[str] = Field(default_factory=list)
    debates: list[str] = Field(default_factory=list)
    from_year: int | None = None
    to_year: int | None = None


class SearchRequest(SearchFilters):
    q: str = ""
    limit: int = None
    include_snippets: bool = True
    speaker: str | None = None  # Exact speaker match if user selects it.
    speaker_ids: list[str] | None = None  # The _key from the people collection

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
    snippet: str | None = None  # Add default to make validation more forgiving
    snippet_long: str | None = None
    number: int | None = None
    debate_type: str | None = None
    speaker: str | None = None
    date: str | None = None
    year: int | None = None
    url_session: str | None = None
    party: str | None = None
    url_audio: str | None = None
    audio_start_seconds: int | None = None
    person_id: str | None = None

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
    results: list[TalkHit]
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
    messages: list[ChatTurn]
    limit: int = 5  # semantic context size


class ChatResponse(BaseModel):
    reply: str
    citations: list[TalkHit]
