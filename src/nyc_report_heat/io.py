from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import pandas as pd

from nyc_report_heat.models import Candidate, RankedItem
from nyc_report_heat.heat import heat_score
from nyc_report_heat.urltools import normalize_url


def write_candidates(path: Path, candidates: Iterable[Candidate]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for candidate in candidates:
            fh.write(candidate.model_dump_json() + "\n")


def read_candidates(path: Path) -> list[Candidate]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        return [Candidate.model_validate_json(line) for line in fh if line.strip()]


def candidate_key(candidate: Candidate) -> str:
    return normalize_url(candidate.heat_url)


def diff_candidates(previous: Iterable[Candidate], current: Iterable[Candidate]) -> list[Candidate]:
    previous_keys = {candidate_key(candidate) for candidate in previous}
    return [candidate for candidate in current if candidate_key(candidate) not in previous_keys]


def merge_candidates(previous: Iterable[Candidate], discovered: Iterable[Candidate]) -> list[Candidate]:
    merged: dict[str, Candidate] = {candidate_key(candidate): candidate for candidate in previous}
    for candidate in discovered:
        key = candidate_key(candidate)
        existing = merged.get(key)
        if existing is None:
            merged[key] = candidate
            continue
        if not existing.published_date and candidate.published_date:
            merged[key] = candidate
    return list(merged.values())


def write_ranked(path: Path, items: Iterable[RankedItem]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    items = list(items)
    with path.open("w", encoding="utf-8") as fh:
        for item in items:
            fh.write(item.model_dump_json() + "\n")
    pd.DataFrame(ranked_rows(items)).to_csv(path.with_suffix(".csv"), index=False)


def _window_sort_key(key: str) -> int:
    return 1 if key == "today" else int(key.removesuffix("d"))


def ranked_rows(items: Iterable[RankedItem]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for item in items:
        candidate = item.candidate
        row = {
            "rank_window": item.rank_window,
            "heat_score": item.heat_score,
            "heat_rank_score": item.heat_rank_score,
            "source": candidate.source_name,
            "kind": candidate.kind,
            "agency": candidate.agency,
            "title": candidate.title,
            "published_date": candidate.published_date,
            "url": candidate.url,
            "document_url": candidate.document_url,
            "format": candidate.format,
            "rationale": " | ".join(item.rationale),
        }
        for key in sorted(item.heat_windows, key=_window_sort_key):
            heat = item.heat_windows[key]
            row[f"heat_score_{key}"] = heat_score(heat)
            row[f"coverage_mentions_{key}"] = heat.coverage_mentions
            row[f"exact_url_mentions_{key}"] = heat.exact_url_mentions
            row[f"filename_mentions_{key}"] = heat.filename_mentions
            row[f"canonical_mentions_{key}"] = heat.canonical_mentions
            row[f"social_exact_mentions_{key}"] = heat.social_exact_mentions
            row[f"crawl_hits_{key}"] = heat.crawl_hits
            row[f"providers_checked_{key}"] = ",".join(heat.providers_checked)
            row[f"errors_{key}"] = " | ".join(heat.errors)
        rows.append(row)
    return rows


def write_ranked_views(output_dir: Path, ranked: list[RankedItem]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    views = {
        "top_reports.csv": [item for item in ranked if item.candidate.kind in {"report", "publication"}],
        "top_rules.csv": [item for item in ranked if item.candidate.kind == "rule"],
        "link_heat.csv": [item for item in ranked if item.heat_score > 0],
    }
    for filename, items in views.items():
        pd.DataFrame(ranked_rows(items)).to_csv(output_dir / filename, index=False)


def write_candidates_csv(path: Path, candidates: Iterable[Candidate]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for candidate in candidates:
        rows.append(
            {
                "source_name": candidate.source_name,
                "kind": candidate.kind,
                "agency": candidate.agency,
                "title": candidate.title,
                "published_date": candidate.published_date,
                "url": candidate.url,
                "document_url": candidate.document_url,
                "format": candidate.format,
                "source_page": candidate.source_page,
            }
        )
    pd.DataFrame(rows).to_csv(path, index=False)


def write_daily_summary(path: Path, ranked: list[RankedItem], new_candidates: list[Candidate]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    generated = datetime.now(timezone.utc).isoformat(timespec="seconds")
    top_reports = [item for item in ranked if item.candidate.kind in {"report", "publication"}][:10]
    top_rules = [item for item in ranked if item.candidate.kind == "rule"][:10]
    link_heat = [item for item in ranked if item.heat_score > 0][:10]
    lines = [
        "# Daily NYC Report Heat Summary",
        "",
        f"Generated: {generated}",
        f"Candidates ranked: {len(ranked)}",
        f"New candidates: {len(new_candidates)}",
        f"Rank window: {ranked[0].rank_window if ranked else 'n/a'}",
        "",
        "## Top Link Heat Overall",
        "",
    ]
    for idx, item in enumerate(ranked[:15], 1):
        c = item.candidate
        lines.append(
            f"{idx}. {c.title} | {c.source_name} | heat {item.heat_score:.1f} | {c.heat_url}"
        )
    lines.extend(["", "## Top Reports/Publications", ""])
    for idx, item in enumerate(top_reports, 1):
        c = item.candidate
        lines.append(
            f"{idx}. {c.title} | {c.source_name} | heat {item.heat_score:.1f} | {c.heat_url}"
        )
    lines.extend(["", "## Top Rules", ""])
    for idx, item in enumerate(top_rules, 1):
        c = item.candidate
        lines.append(
            f"{idx}. {c.title} | {c.source_name} | heat {item.heat_score:.1f} | {c.heat_url}"
        )
    lines.extend(["", "## Link Heat Signals", ""])
    if not link_heat:
        lines.append("No exact link/report heat signals found in enabled providers.")
    else:
        for idx, item in enumerate(link_heat, 1):
            c = item.candidate
            lines.append(
                f"{idx}. {c.title} | {item.rank_window} heat {item.heat_score:.1f} | exact {item.heat.exact_url_mentions} | filename {item.heat.filename_mentions} | social {item.heat.social_exact_mentions} | crawl {item.heat.crawl_hits} | {c.heat_url}"
            )
    lines.extend(["", "## New Candidates", ""])
    if not new_candidates:
        lines.append("No new candidates found.")
    else:
        for candidate in new_candidates[:50]:
            lines.append(
                f"- {candidate.title} | {candidate.source_name} | {candidate.published_date or 'unknown date'} | {candidate.heat_url}"
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _mention_payload(mention) -> dict[str, object]:
    return {
        "provider": mention.provider,
        "title": mention.title,
        "url": mention.url,
        "published_at": mention.published_at.isoformat() if mention.published_at else None,
        "confidence": mention.confidence,
    }


def _window_payload(heat) -> dict[str, object]:
    return {
        "score": heat_score(heat),
        "coverage_mentions": heat.coverage_mentions,
        "exact_url_mentions": heat.exact_url_mentions,
        "canonical_mentions": heat.canonical_mentions,
        "filename_mentions": heat.filename_mentions,
        "social_exact_mentions": heat.social_exact_mentions,
        "crawl_hits": heat.crawl_hits,
        "providers_checked": list(heat.providers_checked),
    }


def _item_payload(item: RankedItem, mention_limit: int = 12) -> dict[str, object]:
    candidate = item.candidate
    windows = {
        key: _window_payload(item.heat_windows[key])
        for key in sorted(item.heat_windows, key=_window_sort_key)
    }
    # Surface evidence from the ranking window first, then any other window, deduped.
    seen: set[tuple] = set()
    mentions: list[dict[str, object]] = []
    ordered_keys = [item.rank_window] + [k for k in windows if k != item.rank_window]
    for key in ordered_keys:
        for mention in item.heat_windows[key].mentions:
            dedup = (mention.provider, mention.url, mention.title)
            if dedup in seen:
                continue
            seen.add(dedup)
            mentions.append(_mention_payload(mention))
            if len(mentions) >= mention_limit:
                break
        if len(mentions) >= mention_limit:
            break
    return {
        "id": candidate.source_id,
        "source": candidate.source_name,
        "source_id": candidate.source_id.split(":", 1)[0],
        "kind": candidate.kind,
        "title": candidate.title,
        "agency": candidate.agency,
        "published_date": candidate.published_date.isoformat() if candidate.published_date else None,
        "url": candidate.url,
        "document_url": candidate.document_url,
        "heat_url": candidate.heat_url,
        "format": candidate.format,
        "source_page": candidate.source_page,
        "rank_window": item.rank_window,
        "heat_score": item.heat_score,
        "rationale": list(item.rationale),
        "windows": windows,
        "mentions": mentions,
    }


def write_dashboard_json(
    path: Path,
    ranked: list[RankedItem],
    new_candidates: Iterable[Candidate] | None = None,
) -> None:
    """Write a single denormalized JSON feed consumed by the static dashboard."""
    path.parent.mkdir(parents=True, exist_ok=True)
    new_candidates = list(new_candidates or [])
    rank_window = ranked[0].rank_window if ranked else "7d"
    window_keys = (
        sorted(ranked[0].heat_windows, key=_window_sort_key) if ranked else ["today", "7d", "30d"]
    )
    providers = sorted(
        {p for item in ranked for heat in item.heat_windows.values() for p in heat.providers_checked}
    )

    kind_counts = Counter(item.candidate.kind for item in ranked)
    source_counts = Counter(item.candidate.source_name for item in ranked)
    format_counts = Counter(item.candidate.format for item in ranked)

    # Provider health for the ranking window: distinguish "checked, found
    # nothing" from "errored", so an all-zero board is not mistaken for genuine
    # silence when a provider was actually unavailable.
    checked = Counter()
    errored = Counter()
    for item in ranked:
        heat = item.heat_windows.get(rank_window)
        if heat is None:
            continue
        for provider in heat.providers_checked:
            checked[provider] += 1
        for err in heat.errors:
            errored[err.split(":", 1)[0]] += 1
    provider_health = [
        {"provider": provider, "checked": checked[provider], "errors": errored.get(provider, 0)}
        for provider in sorted(checked)
    ]

    new_keys = {candidate_key(candidate) for candidate in new_candidates}

    items = []
    for item in ranked:
        payload = _item_payload(item)
        payload["is_new"] = candidate_key(item.candidate) in new_keys
        items.append(payload)

    with_heat = sum(1 for item in ranked if item.heat_score > 0)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rank_window": rank_window,
        "windows": window_keys,
        "providers": providers,
        "stats": {
            "total": len(ranked),
            "with_heat": with_heat,
            "new_count": len(new_candidates),
            "by_kind": dict(kind_counts),
            "by_format": dict(format_counts),
            "by_source": [
                {"name": name, "count": count} for name, count in source_counts.most_common()
            ],
            "provider_health": provider_health,
        },
        "new_candidates": [
            {
                "title": candidate.title,
                "source": candidate.source_name,
                "kind": candidate.kind,
                "published_date": candidate.published_date.isoformat()
                if candidate.published_date
                else None,
                "heat_url": candidate.heat_url,
                "format": candidate.format,
            }
            for candidate in new_candidates
        ],
        "items": items,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_sample_markdown(path: Path, items: list[RankedItem], limit: int = 20) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# NYC Report Heat Sample", ""]
    for rank, item in enumerate(items[:limit], 1):
        c = item.candidate
        lines.extend(
            [
                f"## {rank}. {c.title}",
                "",
                f"- Source: {c.source_name}",
                f"- Agency: {c.agency or 'n/a'}",
                f"- Date: {c.published_date or 'unknown'}",
                f"- Format: {c.format}",
                f"- URL: {c.url}",
                f"- Document URL: {c.document_url or 'n/a'}",
                f"- Rank window: {item.rank_window}",
                f"- Heat score ({item.rank_window}): {item.heat_score}",
                f"- Mentions: exact {item.heat.exact_url_mentions}, canonical {item.heat.canonical_mentions}, filename {item.heat.filename_mentions}, social exact {item.heat.social_exact_mentions}, crawl {item.heat.crawl_hits}",
                f"- Rationale: {'; '.join(item.rationale)}",
                "",
            ]
        )
        evidence = item.heat.mentions[:5]
        if evidence:
            lines.append("Evidence:")
            for mention in evidence:
                lines.append(f"- {mention.provider}: {mention.title or mention.url or mention.query}")
            lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
