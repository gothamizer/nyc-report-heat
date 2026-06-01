from datetime import datetime, timedelta, timezone

from nyc_report_heat.heat import _heat_queries, collect_heat, heat_score, query_gdelt_coverage
from nyc_report_heat.models import Candidate, HeatResult, Mention


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    """Captures the GDELT request and returns a canned article list."""

    def __init__(self, payload):
        self._payload = payload
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs.get("params")))
        return _FakeResponse(self._payload)


def test_gdelt_coverage_counts_recent_articles_and_buckets_by_window() -> None:
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(days=2)).strftime("%Y%m%d%H%M%S")
    old = (now - timedelta(days=20)).strftime("%Y%m%d%H%M%S")
    payload = {
        "articles": [
            {"url": "https://news.example/a", "title": "City reclaims vacant apartments", "seendate": recent},
            {"url": "https://news.example/b", "title": "More vacant units recovered", "seendate": old},
        ]
    }
    client = _FakeClient(payload)
    candidate = Candidate(
        source_id="x", source_name="Test", kind="report",
        title="A Distinctive Report Title About Vacant Apartments",
        url="https://example.com/report",
    )

    # 7d window keeps only the recent article; 30d keeps both.
    count_7d, mentions_7d, errors = query_gdelt_coverage(client, candidate, days=7)
    count_30d, mentions_30d, _ = query_gdelt_coverage(client, candidate, days=30)

    assert errors == []
    assert count_7d == 1
    assert count_30d == 2
    assert all(m.confidence == "title" for m in mentions_30d)
    # The request params do not depend on the window, so the cache can serve both.
    assert client.calls[0][1]["timespan"] == "30d"


def test_collect_heat_routes_title_mentions_to_coverage() -> None:
    now = datetime.now(timezone.utc)
    payload = {"articles": [
        {"url": "https://news.example/a", "title": "Vacant apartments back in use", "seendate": now.strftime("%Y%m%d%H%M%S")},
    ]}
    client = _FakeClient(payload)
    candidate = Candidate(
        source_id="x", source_name="Test", kind="report",
        title="A Distinctive Report Title About Vacant Apartments",
        url="https://example.com/report",
    )
    result = collect_heat(client=client, candidate=candidate, days=7, providers={"gdelt"})
    assert result.coverage_mentions == 1
    assert result.exact_url_mentions == 0
    assert heat_score(result) == 1.0


def test_all_boilerplate_title_is_skipped_to_avoid_noise() -> None:
    client = _FakeClient({"articles": []})
    # Every word is shared government boilerplate -> no distinctive terms -> skip.
    candidate = Candidate(source_id="x", source_name="T", kind="report", title="The Annual Report", url="https://e.com/r")
    count, mentions, errors = query_gdelt_coverage(client, candidate, days=30)
    assert (count, mentions, errors) == (0, [], [])
    assert client.calls == []  # no request issued


def test_distinctive_query_drops_boilerplate_keeps_specifics() -> None:
    from nyc_report_heat.heat import distinctive_query

    q = distinctive_query("DOI's Investigation into the Reclamation of Vacant NYCHA Apartments")
    assert q is not None
    lowered = q.lower()
    assert "investigation" not in lowered and "into" not in lowered
    assert "nycha" in lowered and "vacant" in lowered


def test_pdf_heat_queries_keep_exact_url_before_filename() -> None:
    candidate = Candidate(
        source_id="x",
        source_name="Test",
        kind="report",
        title="Report",
        url="https://example.com/reports/access-denied-long-filename.pdf",
        document_url="https://example.com/reports/access-denied-long-filename.pdf",
        format="pdf",
    )

    queries = _heat_queries(candidate)

    assert queries[0] == (candidate.heat_url, "exact_url")
    assert ("access-denied-long-filename.pdf", "filename") in queries


def test_collect_heat_dedupes_duplicate_mentions(monkeypatch) -> None:
    candidate = Candidate(source_id="x", source_name="Test", kind="report", title="Report", url="https://example.com/report")
    mention = Mention(
        provider="googlenews",
        query=candidate.heat_url,
        url="https://news.example.com/story",
        title="Story",
        confidence="exact_url",
    )

    def fake_google_news(client, candidate, days):
        return 2, [mention, mention], []

    monkeypatch.setattr("nyc_report_heat.heat.query_google_news", fake_google_news)

    result = collect_heat(client=None, candidate=candidate, days=7, providers={"googlenews"})  # type: ignore[arg-type]

    assert result.exact_url_mentions == 1
    assert len(result.mentions) == 1


def test_commoncrawl_is_not_counted_in_windowed_heat(monkeypatch) -> None:
    candidate = Candidate(source_id="x", source_name="Test", kind="report", title="Report", url="https://example.com/report")

    def fake_commoncrawl(client, candidate):
        return 5, []

    monkeypatch.setattr("nyc_report_heat.heat.query_common_crawl", fake_commoncrawl)

    result = collect_heat(client=None, candidate=candidate, days=1, providers={"commoncrawl"})  # type: ignore[arg-type]

    assert result.crawl_hits == 0
    assert result.errors == ["commoncrawl:skipped:not available as a rolling-window heat signal"]
