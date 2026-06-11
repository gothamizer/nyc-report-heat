import math
from datetime import datetime, timedelta, timezone

from nyc_report_heat.heat import heat_from_store, heat_score
from nyc_report_heat.models import Candidate, HarvestedMention


def _harvested(target: str, provider: str = "bluesky", days_ago: int = 2, engagement: int = 0) -> HarvestedMention:
    when = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return HarvestedMention(
        uid=f"{provider}:{target}:{days_ago}",
        provider=provider,
        query="example.com",
        target_url=target,
        source_url="https://post.example/1",
        published_at=when,
        engagement=engagement,
    )


def test_heat_from_store_buckets_by_window_and_provider() -> None:
    candidate = Candidate(
        source_id="x", source_name="Test", kind="report", title="Report",
        url="https://example.com/reports/thing",
    )
    matched = [
        (_harvested("https://example.com/reports/thing", "bluesky", days_ago=2, engagement=12), "exact_url"),
        (_harvested("https://example.com/reports/thing", "newsrss", days_ago=3), "exact_url"),
        (_harvested("https://example.com/reports/thing", "bluesky", days_ago=20, engagement=50), "exact_url"),
    ]

    week = heat_from_store(candidate, matched, days=7)
    month = heat_from_store(candidate, matched, days=30)

    assert week.social_exact_mentions == 1
    assert week.social_engagement == 12
    assert week.exact_url_mentions == 1  # the news link
    assert month.social_exact_mentions == 2
    assert month.social_engagement == 62
    # 6*news + 2*social + log10(1 + engagement)
    assert heat_score(week) == 6.0 + 2.0 + math.log10(1 + 12)


def test_heat_score_engagement_is_log_scaled() -> None:
    candidate = Candidate(source_id="x", source_name="T", kind="report", title="R", url="https://e.com/r")
    # Engagement enters as log10(1 + eng): no arbitrary cap, grows slowly so a
    # single news citation (6) still outweighs any realistic social amplification.
    small = heat_from_store(candidate, [(_harvested("https://e.com/r", "bluesky", days_ago=1, engagement=9), "exact_url")], days=7)
    big = heat_from_store(candidate, [(_harvested("https://e.com/r", "bluesky", days_ago=1, engagement=999), "exact_url")], days=7)
    assert heat_score(small) == 2.0 + math.log10(10)   # 2 + 1
    assert heat_score(big) == 2.0 + math.log10(1000)   # 2 + 3
    assert heat_score(big) < 6.0  # one social post never beats one news citation
