from nyc_report_heat.io import diff_candidates, merge_candidates
from nyc_report_heat.models import Candidate


def candidate(url: str, title: str = "Title") -> Candidate:
    return Candidate(
        source_id=title,
        source_name="Test Source",
        kind="report",
        title=title,
        url=url,
        format="html",
    )


def test_diff_candidates_uses_normalized_heat_url() -> None:
    previous = [candidate("https://example.com/report?utm_source=test")]
    current = [
        candidate("https://example.com/report"),
        candidate("https://example.com/new-report", "New"),
    ]
    assert [item.title for item in diff_candidates(previous, current)] == ["New"]


def test_merge_candidates_keeps_previous_items_not_in_latest_discovery() -> None:
    previous = [
        candidate("https://example.com/old-report", "Old"),
        candidate("https://example.com/report", "Existing"),
    ]
    discovered = [
        candidate("https://example.com/report", "Existing Updated"),
        candidate("https://example.com/new-report", "New"),
    ]
    merged = merge_candidates(previous, discovered)
    assert [item.title for item in merged] == ["Old", "Existing", "New"]
