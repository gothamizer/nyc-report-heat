from nyc_report_heat.discovery import _council_post_candidates


def test_council_post_yields_only_freshly_uploaded_report_pdfs() -> None:
    post = {
        "date": "2025-12-22T12:44:14",
        "link": "https://council.nyc.gov/press/2025/12/22/3036/",
        "title": {"rendered": "NYC Council Investigation of Nearly 200 Public Restrooms &#8212; Findings"},
        "content": {
            "rendered": (
                '<p>Read the <a href="https://council.nyc.gov/press/wp-content/uploads/sites/56/2025/12/'
                'OID_Restrooms-REPORT_121625-v4.pdf">full report</a>, which builds on the '
                '<a href="https://council.nyc.gov/press/wp-content/uploads/sites/56/2024/09/'
                'Parks-BathroomReport.pdf">2024 report</a> and '
                '<a href="https://www.nyc.gov/assets/doh/downloads/pdf/restrooms.pdf">DOHMH data</a>.</p>'
            )
        },
    }

    candidates = _council_post_candidates(post, seen=set())

    # the 2024 back-reference and the external nyc.gov PDF are not this release's document
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.document_url.endswith("OID_Restrooms-REPORT_121625-v4.pdf")
    assert candidate.url == "https://council.nyc.gov/press/2025/12/22/3036"
    assert candidate.title == "NYC Council Investigation of Nearly 200 Public Restrooms — Findings"
    assert candidate.kind == "report"
    assert candidate.format == "pdf"
    assert str(candidate.published_date) == "2025-12-22"
    assert candidate.source_name == "NYC City Council"


def test_adoption_promotes_unmatched_report_like_links() -> None:
    from datetime import datetime, timezone

    from nyc_report_heat.adoption import adopt_from_mentions
    from nyc_report_heat.models import Candidate, HarvestedMention

    def mention(url: str) -> HarvestedMention:
        return HarvestedMention(
            uid=f"bluesky:{url}",
            provider="bluesky",
            query="nyc.gov",
            target_url=url,
            published_at=datetime.now(timezone.utc),
        )

    tracked = Candidate(
        source_id="doi:abc123",
        source_name="NYC Department of Investigation",
        kind="report",
        title="Tracked already",
        url="https://www.nyc.gov/assets/doi/downloads/existing-report.pdf",
        document_url="https://www.nyc.gov/assets/doi/downloads/existing-report.pdf",
        format="pdf",
    )

    adopted = adopt_from_mentions(
        [
            mention("https://comptroller.nyc.gov/reports/latine-fact-sheet/"),
            mention("https://nyc.gov/assets/acs/pdf/data-analysis/flashReports/2026/04.pdf"),
            mention("https://www.nyc.gov/assets/acs/pdf/data-analysis/flashReports/2026/04.pdf"),  # www dupe
            mention("https://nyc.gov/assets/doi/downloads/existing-report.pdf"),  # already tracked (www folded)
            mention("https://www.nyc.gov/mayors-office/news/2026/06/some-presser/"),  # not report-like
            mention("https://council.nyc.gov/press/2026/05/20/3125/"),  # dated permalink: press release
            mention("https://council.nyc.gov/shahana-hanif/2026/05/19/some-member-post/"),  # member blog post
            mention("https://www.nyc.gov/site/immigrants/legal-resources/moia-hotline.page"),  # service page
        ],
        [tracked],
        client=None,
    )

    assert len(adopted) == 2
    fact_sheet, flash = adopted
    assert fact_sheet.source_name == "NYC Comptroller"
    assert fact_sheet.title == "Latine Fact Sheet"
    assert fact_sheet.kind == "report"
    assert fact_sheet.document_url is None
    assert flash.source_name == "NYC Administration for Children's Services"
    assert flash.format == "pdf"
    assert flash.document_url == "https://nyc.gov/assets/acs/pdf/data-analysis/flashReports/2026/04.pdf"
