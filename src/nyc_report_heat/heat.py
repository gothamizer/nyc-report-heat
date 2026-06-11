from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from nyc_report_heat.models import Candidate, HarvestedMention, HeatResult, Mention


def _cutoff(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def heat_from_store(
    candidate: Candidate,
    matched: list[tuple[HarvestedMention, str]],
    days: int,
) -> HeatResult:
    """Build a window's heat from this candidate's harvested-store matches.

    `matched` is [(mention, confidence)] where confidence is exact_url or
    filename, as classified by harvest.match_mention. Every counted mention is
    verifiable evidence — a real post or article carrying the exact link or
    the document's distinctive filename.
    """
    result = HeatResult(candidate_url=candidate.heat_url, window_days=days)
    cutoff = _cutoff(days)
    for mention, confidence in matched:
        if mention.observed_at < cutoff:
            continue
        result.mentions.append(
            Mention(
                provider=mention.provider,
                query=mention.query,
                url=mention.source_url or mention.target_url,
                title=mention.title,
                published_at=mention.published_at,
                confidence="exact_url" if confidence == "exact_url" else "filename",
            )
        )
        if confidence == "filename":
            result.filename_mentions += 1
        elif mention.provider in {"bluesky", "hackernews", "reddit"}:
            result.social_exact_mentions += 1
            result.social_engagement += mention.engagement
        else:
            result.exact_url_mentions += 1
    return result


def heat_score(result: HeatResult) -> float:
    """Objective attention score for the exact report within one window.

    News citations dominate. Each social pickup counts, plus a log-scaled
    bonus for how much those posts were amplified — log(1 + engagement)
    discriminates across the range reports actually see (single digits to a
    few dozen) without an arbitrary cap, and a single news citation (6) still
    outweighs any realistic engagement bonus. Filename-only pickups are
    lower-confidence and capped.
    """
    return (
        6.0 * result.exact_url_mentions
        + 2.0 * result.social_exact_mentions
        + 1.0 * math.log10(1 + result.social_engagement)
        + 2.0 * min(result.filename_mentions, 5)
    )


def heat_window_key(days: int) -> str:
    return "today" if days == 1 else f"{days}d"
