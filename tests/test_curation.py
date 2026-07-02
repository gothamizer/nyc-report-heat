import json
from datetime import datetime, timezone

from nyc_report_heat.curation import (
    CurationRecord,
    append_curation,
    apply_curation_to_candidates,
    apply_curation_to_ranked,
    article_prefilter,
    build_agency_pattern,
    candidate_url_key,
    curate_candidates,
    make_article_matcher,
    read_curation_store,
)
from nyc_report_heat.heat import heat_from_store
from nyc_report_heat.models import Candidate, HarvestedMention
from nyc_report_heat.scoring import rank_candidate


def _candidate(url: str, title: str = "Some Title", kind: str = "report") -> Candidate:
    return Candidate(
        source_id=f"test:{hash(url) & 0xFFFF:x}",
        source_name="NYC Comptroller",
        kind=kind,
        title=title,
        url=url,
        format="pdf" if url.endswith(".pdf") else "html",
    )


def test_store_round_trip(tmp_path):
    path = tmp_path / "curation.jsonl"
    records = [
        CurationRecord(url="https://comptroller.nyc.gov/a.pdf", include=True, title="Audit of A"),
        CurationRecord(url="https://comptroller.nyc.gov/b.pdf", include=False, title="Spanish Translation"),
    ]
    append_curation(path, records)
    store = read_curation_store(path)
    assert store["https://comptroller.nyc.gov/a.pdf"].include is True
    assert store["https://comptroller.nyc.gov/b.pdf"].include is False
    # last write wins on re-curation
    append_curation(path, [CurationRecord(url="https://comptroller.nyc.gov/b.pdf", include=True, title="Fixed")])
    assert read_curation_store(path)["https://comptroller.nyc.gov/b.pdf"].include is True


def test_curate_candidates_appends_verdicts(tmp_path):
    path = tmp_path / "curation.jsonl"
    candidates = [
        _candidate("https://www.nyc.gov/assets/dhs/downloads/pdf/LL-26-FY24Q1.pdf", title="FY24Q1"),
        _candidate("https://www.nyc.gov/assets/dca/downloads/pdf/es-report.pdf", title="Español (Spanish)"),
    ]

    def fake_complete(payload: str, model: str) -> dict:
        items = json.loads(payload)
        assert len(items) == 2
        return {
            "items": [
                {"index": 0, "include": True, "title": "Local Law 26 Shelter Report, FY2024 Q1", "kind": "report", "reason": "mandated report"},
                {"index": 1, "include": False, "title": "Spanish translation", "kind": "report", "reason": "translation duplicate"},
            ]
        }

    new = curate_candidates(candidates, path, complete=fake_complete)
    assert len(new) == 2
    store = read_curation_store(path)
    assert store[candidate_url_key(candidates[0])].title.startswith("Local Law 26")
    assert store[candidate_url_key(candidates[1])].include is False
    # second run: nothing pending, no calls
    assert curate_candidates(candidates, path, complete=fake_complete) == []


def test_apply_overlay_filters_and_retitles(tmp_path):
    keep = _candidate("https://comptroller.nyc.gov/a.pdf", title="011625 Budget Briefing")
    drop = _candidate("https://comptroller.nyc.gov/b.pdf", title="English")
    uncurated = _candidate("https://comptroller.nyc.gov/c.pdf", title="New Doc")
    store = {
        candidate_url_key(keep): CurationRecord(url=candidate_url_key(keep), include=True, title="January 2025 Budget Briefing"),
        candidate_url_key(drop): CurationRecord(url=candidate_url_key(drop), include=False, title="English"),
    }
    ranked = [rank_candidate(c, {"7d": heat_from_store(c, [], days=7)}, "7d") for c in (keep, drop, uncurated)]
    kept, excluded = apply_curation_to_ranked(ranked, store)
    assert excluded == 1
    assert [item.candidate.title for item in kept] == ["January 2025 Budget Briefing", "New Doc"]

    candidates = apply_curation_to_candidates([keep, drop, uncurated], store)
    assert [c.title for c in candidates] == ["January 2025 Budget Briefing", "New Doc"]


def test_article_prefilter():
    pattern = build_agency_pattern([_candidate("https://comptroller.nyc.gov/a.pdf")])
    assert article_prefilter("A new audit from the comptroller found waste.", pattern)
    assert not article_prefilter("The Mets won last night.", pattern)
    assert not article_prefilter("The comptroller attended a parade.", pattern)  # agency, no report word


def test_article_matcher_produces_named_mentions(tmp_path):
    candidate = _candidate("https://comptroller.nyc.gov/reports/storefronts.pdf", title="Who's Minding the Storefronts?")
    calls = []

    def fake_complete(system_blocks, payload, model):
        calls.append(payload)
        return {"matches": [{"url": "https://comptroller.nyc.gov/reports/storefronts.pdf", "quote": "the comptroller's report found"}]}

    matcher = make_article_matcher([candidate], tmp_path / "curation.jsonl", complete=fake_complete)
    article = {
        "key": "https://gothamist.com/news/x",
        "url": "https://gothamist.com/news/x",
        "title": "Storefront vacancies rise",
        "outlet": "gothamist.com",
        "feed_url": "https://gothamist.com/feed",
        "published_at": datetime(2026, 7, 1, tzinfo=timezone.utc),
    }
    mentions = matcher(article, "A new report from the comptroller found storefront vacancies rose.", set())
    assert len(mentions) == 1
    mention = mentions[0]
    assert mention.via == "title"
    assert mention.match_quote == "the comptroller's report found"
    assert mention.target_url == candidate_url_key(candidate)

    # a target the article already linked is not double-counted
    already = {candidate_url_key(candidate).replace("://www.", "://", 1)}
    assert matcher(article, "A new report from the comptroller found more.", already) == []

    # articles failing the prefilter never reach the model
    before = len(calls)
    assert matcher(article, "The Mets won last night.", set()) == []
    assert len(calls) == before


def test_named_mention_counts_in_heat():
    candidate = _candidate("https://comptroller.nyc.gov/reports/storefronts.pdf")
    mention = HarvestedMention(
        uid="newsrss:llmnamed:a:b",
        provider="newsrss",
        query="feed",
        target_url=candidate_url_key(candidate),
        source_url="https://gothamist.com/news/x",
        title="Storefront vacancies rise",
        published_at=datetime.now(timezone.utc),
        via="title",
        match_quote="the report found",
    )
    result = heat_from_store(candidate, [(mention, "named")], days=7)
    assert result.named_mentions == 1
