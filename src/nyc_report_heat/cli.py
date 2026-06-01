from __future__ import annotations

from pathlib import Path
from random import Random
from concurrent.futures import ThreadPoolExecutor, as_completed

import typer
from rich.console import Console
from rich.table import Table

from nyc_report_heat.config import load_settings
from nyc_report_heat.discovery import discover_all, lookback_date
from nyc_report_heat.heat import collect_heat, heat_window_key
from nyc_report_heat.http import HttpClient
from nyc_report_heat.io import (
    diff_candidates,
    merge_candidates,
    read_candidates,
    write_candidates,
    write_candidates_csv,
    write_daily_summary,
    write_dashboard_json,
    write_ranked,
    write_ranked_views,
    write_sample_markdown,
)

DASHBOARD_JSON = Path("site/data/dashboard.json")
from nyc_report_heat.scoring import rank_candidate


app = typer.Typer(no_args_is_help=True)
console = Console()


def _rank_candidates(
    candidates,
    windows: list[int],
    rank_window: str,
    providers: set[str],
    expanded_gdelt_variants: bool,
    request_timeout_seconds: int,
    request_sleep_seconds: float,
    max_workers: int,
) -> list:
    def work(candidate):
        client = HttpClient(timeout=request_timeout_seconds, sleep_seconds=request_sleep_seconds)
        heat_windows = {}
        for days in windows:
            heat_windows[heat_window_key(days)] = collect_heat(
                client,
                candidate,
                days=days,
                providers=providers,
                expanded_gdelt_variants=expanded_gdelt_variants,
            )
        return rank_candidate(candidate, heat_windows, rank_window)

    ranked = []
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as executor:
        futures = {executor.submit(work, candidate): candidate for candidate in candidates}
        completed = 0
        for future in as_completed(futures):
            completed += 1
            candidate = futures[future]
            console.print(f"[dim]{completed}/{len(candidates)}[/dim] {candidate.source_name}: {candidate.title[:90]}")
            ranked.append(future.result())
    ranked.sort(key=lambda item: item.heat_rank_score, reverse=True)
    return ranked


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
def rank(
    candidates_path: Path = Path("data/candidates.jsonl"),
    output: Path = Path("outputs/ranked.jsonl"),
    limit: int = 0,
    windows: str = "",
    rank_window: str = "",
    providers: str = "",
    expanded_gdelt_variants: bool = False,
    sample: bool = False,
    seed: int = 7,
    config: Path | None = Path("config/default.yml"),
    max_workers: int | None = None,
) -> None:
    settings = load_settings(config)
    provider_set = {part.strip().lower() for part in providers.split(",") if part.strip()} or set(settings.providers)
    window_days = _parse_windows(windows, settings.windows)
    selected_rank_window = rank_window or settings.rank_window
    available_windows = {heat_window_key(days) for days in window_days}
    if selected_rank_window not in available_windows:
        raise typer.BadParameter(f"rank_window must be one of: {', '.join(sorted(available_windows))}")
    candidates = read_candidates(candidates_path)
    if sample and limit > 0 and len(candidates) > limit:
        candidates = Random(seed).sample(candidates, limit)
    elif limit > 0:
        candidates = candidates[:limit]
    items = _rank_candidates(
        candidates,
        windows=window_days,
        rank_window=selected_rank_window,
        providers=provider_set,
        expanded_gdelt_variants=expanded_gdelt_variants,
        request_timeout_seconds=settings.request_timeout_seconds,
        request_sleep_seconds=settings.request_sleep_seconds,
        max_workers=max_workers or settings.max_workers,
    )
    write_ranked(output, items)
    write_ranked_views(output.parent, items)
    write_sample_markdown(output.with_name("sample_report.md"), items)
    write_dashboard_json(DASHBOARD_JSON, items)
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
    write_candidates(candidates_path, current)
    write_candidates(new_path, new_candidates)
    write_candidates_csv(new_path.with_suffix(".csv"), new_candidates)

    provider_set = {provider.lower() for provider in settings.providers}
    ranked = _rank_candidates(
        current,
        windows=settings.windows,
        rank_window=settings.rank_window,
        providers=provider_set,
        expanded_gdelt_variants=settings.expanded_gdelt_variants,
        request_timeout_seconds=settings.request_timeout_seconds,
        request_sleep_seconds=settings.request_sleep_seconds,
        max_workers=settings.max_workers,
    )
    write_ranked(ranked_path, ranked)
    write_ranked_views(ranked_path.parent, ranked)
    write_sample_markdown(ranked_path.with_name("sample_report.md"), ranked)
    write_daily_summary(summary_path, ranked, new_candidates)
    write_dashboard_json(DASHBOARD_JSON, ranked, new_candidates)
    console.print(
        f"Discovered {len(discovered)} candidates, tracked {len(current)}, found {len(new_candidates)} new, and ranked {len(ranked)}."
    )


@app.command()
def show(path: Path = Path("outputs/ranked.jsonl"), limit: int = 20) -> None:
    from nyc_report_heat.models import RankedItem

    with path.open(encoding="utf-8") as fh:
        items = [RankedItem.model_validate_json(line) for line in fh if line.strip()]
    table = Table("Rank", "Heat", "Exact URL", "Filename", "Social", "Crawl", "Source", "Title", "URL")
    for idx, item in enumerate(items[:limit], 1):
        table.add_row(
            str(idx),
            f"{item.heat_score:.1f}",
            str(item.heat.exact_url_mentions),
            str(item.heat.filename_mentions),
            str(item.heat.social_exact_mentions),
            str(item.heat.crawl_hits),
            item.candidate.source_name,
            item.candidate.title[:72],
            item.candidate.heat_url,
        )
    console.print(table)
