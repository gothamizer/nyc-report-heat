from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl


SourceKind = Literal["report", "rule", "publication"]
FormatKind = Literal["pdf", "html", "docx", "xlsx", "unknown"]


class Candidate(BaseModel):
    source_id: str
    source_name: str
    kind: SourceKind
    title: str
    agency: str | None = None
    url: str
    document_url: str | None = None
    published_date: date | None = None
    summary: str | None = None
    format: FormatKind = "unknown"
    tags: list[str] = Field(default_factory=list)
    source_page: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def heat_url(self) -> str:
        return self.document_url or self.url


class Mention(BaseModel):
    provider: str
    query: str
    url: str | None = None
    title: str | None = None
    published_at: datetime | None = None
    confidence: Literal["exact_url", "filename", "redirect_or_canonical", "title"] = "exact_url"


class HeatResult(BaseModel):
    candidate_url: str
    window_days: int
    exact_url_mentions: int = 0
    filename_mentions: int = 0
    canonical_mentions: int = 0
    social_exact_mentions: int = 0
    coverage_mentions: int = 0
    crawl_hits: int = 0
    providers_checked: list[str] = Field(default_factory=list)
    mentions: list[Mention] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class RankedItem(BaseModel):
    candidate: Candidate
    heat_windows: dict[str, HeatResult]
    rank_window: str
    heat_score: float
    heat_rank_score: float
    rationale: list[str] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def heat(self) -> HeatResult:
        return self.heat_windows[self.rank_window]
