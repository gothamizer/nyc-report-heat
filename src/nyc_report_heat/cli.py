from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from nyc_report_heat.adoption import adopt_from_mentions
from nyc_report_heat.config import Settings, load_settings
from nyc_report_heat.discovery import discover_all, lookback_date
from nyc_report_heat.harvest import (
    append_mentions,
    build_candidate_index,
    harvest_bluesky,
    harvest_hackernews,
    harvest_news_feeds,
    harvest_reddit,
    match_mention,
    read_mention_store,
)
from nyc_report_heat.heat import heat_from_store, heat_window_key
from nyc_report_heat.http import HttpClient
from nyc_report_heat.io import (
    diff_candidates,
    merge_candidates,
    read_candidates,
    summarize_untracked,
    write_candidates,
    write_candidates_csv,
    write_daily_summary,
    write_dashboard_json,
    write_ranked,
    write_ranked_views,
    write_sample_markdown,
)
from nyc_report_heat.scoring import rank_candidate

DASHBOARD_JSON = Path("site/data/dashboard.json")
MENTIONS_STORE = Path("data/mentions.jsonl")
ARTICLE_LEDGER = Path("data/articles_seen.txt")


app = typer.Typer(no_args_is_help=True)
console = Console()


def _run_harvest(settings: Settings, store_path: Path = MENTIONS_STORE) -> None:
    """Query each enabled provider once per tracked gov domain and append every
    gov link found to the persistent mention store."""
    client = HttpClient(timeout=settings.request_timeout_seconds, sleep_seconds=settings.request_sleep_seconds)
    providers = {provider.lower() for provider in settings.providers}
    harvested = []
    errors: list[str] = []
    if "bluesky" in providers:
        mentions, errs = harvest_bluesky(client, settings.harvest_domains, settings.harvest_lookback_days)
        console.print(f"bluesky: {len(mentions)} gov-link mentions ({len(errs)} errors)")
        harvested.extend(mentions)
        errors.extend(errs)
    if "hackernews" in providers:
        mentions, errs = harvest_hackernews(client, settings.harvest_domains, settings.harvest_lookback_days)
        console.print(f"hackernews: {len(mentions)} gov-link mentions ({len(errs)} errors)")
        harvested.extend(mentions)
        errors.extend(errs)
    if "reddit" in providers:
        mentions, errs = harvest_reddit(client, settings.harvest_domains, settings.harvest_lookback_days)
        console.print(f"reddit: {len(mentions)} gov-link mentions ({len(errs)} errors)")
        harvested.extend(mentions)
        errors.extend(errs)
    if "newsrss" in providers:
        mentions, errs = harvest_news_feeds(
            client,
            settings.news_feeds,
            settings.harvest_domains,
            ledger_path=ARTICLE_LEDGER,
            lookback_days=settings.harvest_lookback_days,
        )
        console.print(f"newsrss: {len(mentions)} gov-link mentions ({len(errs)} errors)")
        harvested.extend(mentions)
        errors.extend(errs)
    added, total = append_mentions(store_path, harvested)
    console.print(f"Mention store: +{added} new, {total} total ({store_path})")
    for err in errors[:10]:
        console.print(f"[dim]warn: {err}[/dim]")


def _score_candidates(
    candidates,
    windows: list[int],
    rank_window: str,
    providers: set[str],
    store_path: Path = MENTIONS_STORE,
):
    """Score every tracked candidate from the harvested mention store. Fully
    local — every counted mention is stored evidence. Returns (ranked, untracked)."""
    store = read_mention_store(store_path)
    by_url, by_filename = build_candidate_index(candidates)
    matches: dict[str, list] = defaultdict(list)
    unmatched = []
    for mention in store:
        if mention.provider not in providers:
            continue
        key, confidence = match_mention(mention, by_url, by_filename)
        if key is None:
            unmatched.append(mention)
        else:
            matches[key].append((mention, confidence))

    from nyc_report_heat.io import candidate_key

    harvest_providers = sorted(providers & {"bluesky", "hackernews", "newsrss", "reddit"})

    ranked = []
    for candidate in candidates:
        candidate_matches = matches.get(candidate_key(candidate), [])
        heat_windows = {}
        for days in windows:
            result = heat_from_store(candidate, candidate_matches, days=days)
            result.providers_checked = list(harvest_providers)
            heat_windows[heat_window_key(days)] = result
        ranked.append(rank_candidate(candidate, heat_windows, rank_window))
    ranked.sort(key=lambda item: item.heat_rank_score, reverse=True)
    untracked = summarize_untracked(unmatched, rank_window)
    return ranked, untracked


def _parse_windows(value: str, fallback: list[int]) -> list[int]:
    if not value:
        return fallback
    windows = sorted({int(part.strip()) for part in value.split(",") if part.strip()})
    if not windows:
        raise typer.BadParameter("windows must include at least one positive integer")
    if any(days < 1 for days in windows):
        raise typer.BadParameter("windows must be positive day counts")
    return windows


@app.command()
def discover(
    output: Path = Path("data/candidates.jsonl"),
    per_source: int = 0,
    lookback_days: int | None = None,
    config: Path | None = Path("config/default.yml"),
) -> None:
    settings = load_settings(config)
    selected_lookback = settings.discovery_lookback_days if lookback_days is None else lookback_days
    client = HttpClient(timeout=settings.request_timeout_seconds, sleep_seconds=settings.request_sleep_seconds)
    candidates = discover_all(
        client=client,
        per_source=per_source or settings.per_source,
        source_ids=settings.source_ids,
        discovered_after=lookback_date(selected_lookback),
    )
    write_candidates(output, candidates)
    console.print(f"Wrote {len(candidates)} candidates to {output}")


@app.command()
def harvest(
    config: Path | None = Path("config/default.yml"),
    lookback_days: int | None = None,
) -> None:
    """Harvest gov-domain mentions from all enabled providers into the store."""
    settings = load_settings(config)
    if lookback_days is not None:
        settings.harvest_lookback_days = lookback_days
    _run_harvest(settings)


@app.command()
def rank(
    candidates_path: Path = Path("data/candidates.jsonl"),
    output: Path = Path("outputs/ranked.jsonl"),
    limit: int = 0,
    windows: str = "",
    rank_window: str = "",
    providers: str = "",
    config: Path | None = Path("config/default.yml"),
) -> None:
    """Score the tracked inventory from the harvested mention store."""
    settings = load_settings(config)
    provider_set = {part.strip().lower() for part in providers.split(",") if part.strip()} or set(settings.providers)
    window_days = _parse_windows(windows, settings.windows)
    selected_rank_window = rank_window or settings.rank_window
    available_windows = {heat_window_key(days) for days in window_days}
    if selected_rank_window not in available_windows:
        raise typer.BadParameter(f"rank_window must be one of: {', '.join(sorted(available_windows))}")
    candidates = read_candidates(candidates_path)
    if limit > 0:
        candidates = candidates[:limit]
    items, untracked = _score_candidates(
        candidates,
        windows=window_days,
        rank_window=selected_rank_window,
        providers=provider_set,
    )
    write_ranked(output, items)
    write_ranked_views(output.parent, items)
    write_sample_markdown(output.with_name("sample_report.md"), items)
    write_dashboard_json(DASHBOARD_JSON, items, untracked=untracked)
    console.print(f"Wrote ranked output to {output} and {output.with_suffix('.csv')}")
    console.print(f"Wrote dashboard feed to {DASHBOARD_JSON}")


@app.command()
def daily(
    config: Path = Path("config/default.yml"),
    candidates_path: Path = Path("data/candidates.jsonl"),
    ranked_path: Path = Path("outputs/ranked.jsonl"),
    new_path: Path = Path("outputs/new_candidates.jsonl"),
    summary_path: Path = Path("outputs/daily_summary.md"),
) -> None:
    settings = load_settings(config)
    previous = read_candidates(candidates_path)
    client = HttpClient(timeout=settings.request_timeout_seconds, sleep_seconds=settings.request_sleep_seconds)
    discovered = discover_all(
        client=client,
        per_source=settings.per_source,
        source_ids=settings.source_ids,
        discovered_after=lookback_date(settings.daily_discovery_lookback_days),
    )
    new_candidates = diff_candidates(previous, discovered)
    current = merge_candidates(previous, discovered)

    _run_harvest(settings)

    # Adoption closes the discovery gap from the other side: report-like gov
    # links people are sharing that no listing scrape surfaced get promoted
    # into the inventory instead of sitting in the untracked list.
    adopted = adopt_from_mentions(read_mention_store(MENTIONS_STORE), current, client)
    if adopted:
        current = merge_candidates(current, adopted)
        new_candidates = new_candidates + adopted

    write_candidates(candidates_path, current)
    write_candidates(new_path, new_candidates)
    write_candidates_csv(new_path.with_suffix(".csv"), new_candidates)

    provider_set = {provider.lower() for provider in settings.providers}
    ranked, untracked = _score_candidates(
        current,
        windows=settings.windows,
        rank_window=settings.rank_window,
        providers=provider_set,
    )
    write_ranked(ranked_path, ranked)
    write_ranked_views(ranked_path.parent, ranked)
    write_sample_markdown(ranked_path.with_name("sample_report.md"), ranked)
    write_daily_summary(summary_path, ranked, new_candidates, untracked=untracked)
    write_dashboard_json(DASHBOARD_JSON, ranked, new_candidates, untracked=untracked)
    console.print(
        f"Discovered {len(discovered)} candidates, tracked {len(current)}, found {len(new_candidates)} new, and ranked {len(ranked)}."
    )


@app.command()
def show(path: Path = Path("outputs/ranked.jsonl"), limit: int = 20) -> None:
    from nyc_report_heat.models import RankedItem

    with path.open(encoding="utf-8") as fh:
        items = [RankedItem.model_validate_json(line) for line in fh if line.strip()]
    table = Table("Rank", "Heat", "News links", "Social", "Engage", "Filename", "Source", "Title")
    for idx, item in enumerate(items[:limit], 1):
        table.add_row(
            str(idx),
            f"{item.heat_score:.1f}",
            str(item.heat.exact_url_mentions),
            str(item.heat.social_exact_mentions),
            str(item.heat.social_engagement),
            str(item.heat.filename_mentions),
            item.candidate.source_name,
            item.candidate.title[:72],
        )
    console.print(table)
