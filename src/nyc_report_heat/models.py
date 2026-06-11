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
    confidence: Literal["exact_url", "filename"] = "exact_url"


class HarvestedMention(BaseModel):
    """One public pickup of a tracked-government-domain link, as stored in the
    persistent mention store (data/mentions.jsonl).

    Harvesting is domain-first: providers are queried per gov domain, not per
    candidate, and every gov link found is stored whether or not it matches the
    tracked inventory. `uid` makes appends idempotent across daily runs.
    """

    uid: str
    provider: str  # bluesky | hackernews | newsrss
    query: str
    target_url: str  # the gov link that was shared (normalized)
    source_url: str | None = None  # the post/article that shared it
    title: str | None = None
    author: str | None = None
    published_at: datetime | None = None
    engagement: int = 0  # likes+reposts+quotes+replies / points+comments
    harvested_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def observed_at(self) -> datetime:
        return self.published_at or self.harvested_at


class HeatResult(BaseModel):
    candidate_url: str
    window_days: int
    exact_url_mentions: int = 0
    filename_mentions: int = 0
    social_exact_mentions: int = 0
    social_engagement: int = 0
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
