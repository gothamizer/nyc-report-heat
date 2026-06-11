from datetime import datetime, timezone
from pathlib import Path

from nyc_report_heat.harvest import (
    append_mentions,
    build_candidate_index,
    match_mention,
    read_mention_store,
    target_matches_domains,
)
from nyc_report_heat.models import Candidate, HarvestedMention


DOMAINS = ["comptroller.nyc.gov", "rules.cityofnewyork.us", "nyc.gov"]


def mention(target: str, uid: str = "u1") -> HarvestedMention:
    return HarvestedMention(
        uid=uid,
        provider="bluesky",
        query="comptroller.nyc.gov",
        target_url=target,
        published_at=datetime.now(timezone.utc),
    )


def test_target_matches_domains_handles_subdomains_www_and_path_prefixes() -> None:
    assert target_matches_domains("https://comptroller.nyc.gov/reports/x", DOMAINS)
    assert target_matches_domains("https://www.nyc.gov/assets/doi/reports/pdf/x.pdf", DOMAINS)
    assert target_matches_domains("https://a860-gpp.nyc.gov/doc/123", DOMAINS)  # subdomain of nyc.gov
    assert not target_matches_domains("https://nycgov.example.com/x", DOMAINS)
    assert target_matches_domains("https://nyc.gov/assets/x", ["nyc.gov/assets"])
    assert not target_matches_domains("https://nyc.gov/311", ["nyc.gov/assets"])


def test_mention_store_appends_are_idempotent(tmp_path: Path) -> None:
    store = tmp_path / "mentions.jsonl"
    first = [mention("https://comptroller.nyc.gov/reports/a", "u1")]
    added, total = append_mentions(store, first)
    assert (added, total) == (1, 1)
    # Same uid again plus one new -> only the new one lands.
    again = [mention("https://comptroller.nyc.gov/reports/a", "u1"), mention("https://comptroller.nyc.gov/reports/b", "u2")]
    added, total = append_mentions(store, again)
    assert (added, total) == (1, 2)
    assert {m.uid for m in read_mention_store(store)} == {"u1", "u2"}


def test_match_mention_exact_and_filename_and_none() -> None:
    candidates = [
        Candidate(
            source_id="doi:1", source_name="DOI", kind="report", title="R1",
            url="https://www.nyc.gov/site/doi/report-page",
            document_url="https://www.nyc.gov/assets/doi/reports/pdf/2026/10ACSReport.Release05.05.2026FINAL.pdf",
            format="pdf",
        ),
        Candidate(
            source_id="rules:1", source_name="Rules", kind="rule", title="R2",
            url="https://rules.cityofnewyork.us/rule/lead-dust/",
        ),
    ]
    by_url, by_filename = build_candidate_index(candidates)

    # Exact document URL with tracking junk and http scheme still matches.
    key, conf = match_mention(
        mention("http://www.nyc.gov/assets/doi/reports/pdf/2026/10ACSReport.Release05.05.2026FINAL.pdf?utm_source=t"),
        by_url, by_filename,
    )
    assert conf == "exact_url"

    # The candidate's HTML page URL matches too (trailing slash normalized).
    key2, conf2 = match_mention(mention("https://rules.cityofnewyork.us/rule/lead-dust"), by_url, by_filename)
    assert conf2 == "exact_url"
    assert key2 == "https://rules.cityofnewyork.us/rule/lead-dust"

    # A distinctive PDF filename hosted elsewhere matches at filename confidence.
    key3, conf3 = match_mention(
        mention("https://mirror.example.org/files/10ACSReport.Release05.05.2026FINAL.pdf"),
        by_url, by_filename,
    )
    assert conf3 == "filename"
    assert key3 == key

    # Unrelated gov link -> no match.
    key4, conf4 = match_mention(mention("https://comptroller.nyc.gov/reports/other-report"), by_url, by_filename)
    assert (key4, conf4) == (None, "none")


def test_named_in_print_tier() -> None:
    from nyc_report_heat.harvest import build_title_index, match_mention, named_in_article
    from nyc_report_heat.heat import heat_from_store, heat_score
    from nyc_report_heat.models import Candidate, HarvestedMention

    fifa = Candidate(
        source_id="council:abc",
        source_name="NYC City Council",
        kind="report",
        title="Taken for a Ride",
        url="https://council.nyc.gov/press/2026/06/08/3129",
        document_url="https://council.nyc.gov/press/wp-content/uploads/sites/56/2026/06/taken-for-a-ride_june-2026.pdf",
        format="pdf",
    )
    generic = Candidate(
        source_id="dhs:xyz",
        source_name="NYC Department of Homeless Services",
        kind="report",
        title="Daily Report",  # too generic: excluded from the title index
        url="https://nyc.gov/assets/dhs/downloads/pdf/dailyreport.pdf",
        format="pdf",
    )
    index = build_title_index([fifa, generic])
    assert len(index) == 1  # only the distinctive multi-word title qualifies

    article = (
        "A new 26-page City Council report titled “Taken for a Ride” says "
        "tourists should beware of scalpers and unlicensed cabbies."
    )
    assert named_in_article(article, index, set()) == [
        "https://council.nyc.gov/press/wp-content/uploads/sites/56/2026/06/taken-for-a-ride_june-2026.pdf"
    ]
    # idiom without the publishing body named -> no match
    assert named_in_article("They were taken for a ride by the dealer.", index, set()) == []
    # article already links the document -> no double count
    assert named_in_article(article, index, {index[0][2]}) == []

    mention = HarvestedMention(
        uid="newsrss:named:article:target",
        provider="newsrss",
        query="https://pix11.com/feed/",
        target_url=index[0][2],
        source_url="https://pix11.com/news/local-news/nyc-council-calls-for-education-on-fifa-tourist-schemes/",
        title="NYC Council calls for education on FIFA tourist schemes",
        via="title",
    )
    from nyc_report_heat.harvest import build_candidate_index

    by_url, by_filename = build_candidate_index([fifa, generic])
    key, confidence = match_mention(mention, by_url, by_filename)
    assert confidence == "named"
    assert key is not None

    heat = heat_from_store(fifa, [(mention, confidence)], days=30)
    assert heat.named_mentions == 1
    assert heat.exact_url_mentions == 0
    assert heat_score(heat) == 3.0
    assert heat.mentions[0].confidence == "named"
