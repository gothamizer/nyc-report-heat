from __future__ import annotations

from nyc_report_heat.heat import heat_score
from nyc_report_heat.models import Candidate, HeatResult, RankedItem


def rank_candidate(candidate: Candidate, heat_windows: dict[str, HeatResult], rank_window: str) -> RankedItem:
    heat = heat_windows[rank_window]
    h_score = heat_score(heat)
    reasons: list[str] = []
    if heat.exact_url_mentions:
        plural = "s" if heat.exact_url_mentions != 1 else ""
        reasons.append(f"{heat.exact_url_mentions} news article{plural} linking the exact report URL")
    if heat.social_exact_mentions:
        plural = "s" if heat.social_exact_mentions != 1 else ""
        engagement = f" with {heat.social_engagement} total engagement" if heat.social_engagement else ""
        reasons.append(f"{heat.social_exact_mentions} social post{plural} sharing the exact link{engagement}")
    if heat.filename_mentions:
        reasons.append("report filename pickups found; lower-confidence heat")
    if not reasons:
        reasons.append("no public link pickups found in checked sources")
    return RankedItem(
        candidate=candidate,
        heat_windows=heat_windows,
        rank_window=rank_window,
        heat_score=h_score,
        heat_rank_score=h_score,
        rationale=reasons,
    )
