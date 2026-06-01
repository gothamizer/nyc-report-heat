from nyc_report_heat.heat import heat_window_key
from nyc_report_heat.models import Candidate, HeatResult
from nyc_report_heat.scoring import rank_candidate


def test_rank_candidate_uses_selected_heat_window() -> None:
    candidate = Candidate(
        source_id="x",
        source_name="Test",
        kind="report",
        title="Report",
        url="https://example.com/report",
    )
    ranked = rank_candidate(
        candidate,
        {
            "today": HeatResult(candidate_url=candidate.url, window_days=1, exact_url_mentions=0),
            "7d": HeatResult(candidate_url=candidate.url, window_days=7, exact_url_mentions=2),
            "30d": HeatResult(candidate_url=candidate.url, window_days=30, exact_url_mentions=5),
        },
        "7d",
    )
    assert ranked.rank_window == "7d"
    assert ranked.heat_score == 12.0
    assert ranked.heat.exact_url_mentions == 2


def test_heat_window_key() -> None:
    assert heat_window_key(1) == "today"
    assert heat_window_key(7) == "7d"
