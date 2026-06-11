import json

from nyc_report_heat.io import write_dashboard_json
from nyc_report_heat.models import Candidate, HeatResult, Mention
from nyc_report_heat.scoring import rank_candidate


def candidate(url: str, title: str = "Title", kind: str = "report", fmt: str = "pdf") -> Candidate:
    return Candidate(
        source_id=f"doi:{abs(hash(url)) % 10**8}",
        source_name="NYC Department of Investigation",
        kind=kind,
        title=title,
        url=url,
        document_url=url,
        format=fmt,
    )


def ranked(url: str, exact: int = 0, title: str = "Title", kind: str = "report"):
    windows = {}
    for key, days in [("today", 1), ("7d", 7), ("30d", 30)]:
        heat = HeatResult(candidate_url=url, window_days=days, providers_checked=["newsrss"])
        if key in ("7d", "30d") and exact:
            heat.exact_url_mentions = exact
            heat.mentions = [
                Mention(provider="newsrss", query=url, url="https://news/story", title="Story", confidence="exact_url")
            ]
        windows[key] = heat
    return rank_candidate(candidate(url, title, kind), windows, "7d")


def test_dashboard_feed_shape_and_stats(tmp_path) -> None:
    items = [
        ranked("https://nyc.gov/a.pdf", exact=2, title="Hot one"),
        ranked("https://nyc.gov/b.pdf", exact=0, title="Quiet one"),
        ranked("https://nyc.gov/c.html", exact=0, title="A rule", kind="rule"),
    ]
    new = [items[0].candidate]
    out = tmp_path / "dashboard.json"
    write_dashboard_json(out, items, new)

    data = json.loads(out.read_text())
    assert set(data) >= {"generated_at", "rank_window", "windows", "providers", "stats", "items", "new_candidates"}
    assert data["rank_window"] == "7d"
    assert data["windows"] == ["today", "7d", "30d"]
    assert data["providers"] == ["newsrss"]

    stats = data["stats"]
    assert stats["total"] == 3
    assert stats["with_heat"] == 1  # only the exact-mention item scores in 7d
    assert stats["new_count"] == 1
    assert stats["by_kind"] == {"report": 2, "rule": 1}

    hot = data["items"][0]
    assert hot["title"] == "Hot one"
    assert hot["is_new"] is True
    assert hot["windows"]["7d"]["score"] == 12.0  # 6 * 2 exact mentions
    assert hot["windows"]["today"]["score"] == 0.0
    assert len(hot["mentions"]) == 1
    assert hot["mentions"][0]["provider"] == "newsrss"

    # source id is collapsed to its prefix for grouping
    assert hot["source_id"] == "doi"


def test_untracked_is_report_like_and_deduped() -> None:
    from datetime import datetime, timezone

    from nyc_report_heat.io import summarize_untracked
    from nyc_report_heat.models import HarvestedMention

    def mention(url: str, provider: str = "bluesky", engagement: int = 0) -> HarvestedMention:
        return HarvestedMention(
            uid=f"{provider}:{url}:{engagement}",
            provider=provider,
            query="nyc.gov",
            target_url=url,
            engagement=engagement,
            published_at=datetime.now(timezone.utc),
        )

    rows = summarize_untracked(
        [
            # report-like: kept, and the www/non-www pair collapses to one row
            mention("https://nyc.gov/assets/omb/downloads/pdf/exec26/erc5-26.pdf"),
            mention("https://www.nyc.gov/assets/omb/downloads/pdf/exec26/erc5-26.pdf", "newsrss"),
            mention("https://comptroller.nyc.gov/reports/latine-fact-sheet", engagement=5),
            # not report-like: dropped
            mention("https://housingconnect.nyc.gov/PublicWeb/details/7554"),  # lottery listing
            mention("https://www.nyc.gov/mayors-office/news/2026/06/some-presser"),  # press release
            mention("https://www.nyc.gov/main/events?permalinkName=x&id=1"),  # event listing
            mention("https://www.nyc.gov/content/visionzero/pages"),  # section home
            mention("https://www.nyc.gov/site/fdny/index.page"),  # agency homepage
            mention("https://osc.ny.gov/jobs/events"),  # job board
            mention("https://access.nyc.gov/programs/one-shot-deal"),  # service portal
        ],
        rank_window="30d",
    )

    urls = [row["target_url"] for row in rows]
    assert urls == [
        "https://nyc.gov/assets/omb/downloads/pdf/exec26/erc5-26.pdf",
        "https://comptroller.nyc.gov/reports/latine-fact-sheet",
    ]
    merged = rows[0]
    assert merged["mentions"] == 2
    assert merged["providers"] == ["bluesky", "newsrss"]
