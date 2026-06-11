from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class Settings(BaseModel):
    per_source: int = 250
    discovery_lookback_days: int = 90
    daily_discovery_lookback_days: int = 14
    windows: list[int] = Field(default_factory=lambda: [1, 7, 30])
    rank_window: str = "7d"
    request_timeout_seconds: int = 10
    request_sleep_seconds: float = 0.0
    max_workers: int = 8
    providers: list[str] = Field(default_factory=lambda: ["bluesky", "hackernews", "newsrss"])
    source_ids: list[str] = Field(
        default_factory=lambda: [
            "doi",
            "nyc_comptroller",
            "nys_comptroller",
            "ibo",
            "rules_proposed",
            "rules_adopted",
            "gpp",
        ]
    )
    # Domain-first harvesting: providers are queried once per entry here, and
    # every gov link found is matched against the inventory locally. Entries
    # may carry a path prefix to narrow broad hosts when matching link targets.
    harvest_domains: list[str] = Field(
        default_factory=lambda: [
            "comptroller.nyc.gov",
            "rules.cityofnewyork.us",
            "ibo.nyc.ny.us",
            "osc.ny.gov",
            "a860-gpp.nyc.gov",
            "nyc.gov",
        ]
    )
    news_feeds: list[str] = Field(
        default_factory=lambda: [
            "https://gothamist.com/feed",
            "https://www.thecity.nyc/rss/index.xml",
            "https://www.cityandstateny.com/rss/all/",
            "https://nypost.com/metro/feed/",
            "https://rss.nytimes.com/services/xml/rss/nyt/NYRegion.xml",
            "https://www.amny.com/feed/",
            "https://citylimits.org/feed/",
        ]
    )
    harvest_lookback_days: int = 30


def load_settings(path: Path | None = None) -> Settings:
    if path is None or not path.exists():
        return Settings()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Settings.model_validate(data)
