"""LLM curation: automated inventory triage, retitling, and article matching.

Discovery and adoption are recall-oriented, so the raw inventory picks up
translation duplicates, cryptic filename titles ("Mv En Us 084Sum"), and
non-report pages. Instead of a human reviewing edge cases daily, every tracked
document is triaged once by a small Claude model: keep or exclude, a proper
display title, and a corrected kind. Verdicts persist in data/curation.jsonl
keyed by canonical URL, so each document costs a fraction of a cent, once ever.

The same model also closes the named-in-print recall gap: articles from the
curated NYC feeds that discuss a tracked report without linking it are matched
to the specific document. Every match stays verifiable evidence (a real
article a reader can open and see the report named); the model only replaces
the brittle verbatim-title regex, never invents a citation. The quote that
names the document is stored on the mention for auditing.

Both stages read ANTHROPIC_API_KEY from the environment and skip cleanly
without it, like the Reddit provider.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel, Field

from nyc_report_heat.models import Candidate, HarvestedMention, RankedItem
from nyc_report_heat.urltools import normalize_url

CURATION_STORE = Path("data/curation.jsonl")
CURATION_MODEL = "claude-haiku-4-5"
# Cost guards: at ~600 input / ~60 output tokens per document, a full run of
# 400 documents is under $0.40 on Haiku. The backlog drains across daily runs.
MAX_CURATED_PER_RUN = 400
TRIAGE_BATCH_SIZE = 20
MAX_LLM_ARTICLES_PER_RUN = 30
ARTICLE_TEXT_LIMIT = 8000
SHORTLIST_LIMIT = 250


class CurationRecord(BaseModel):
    url: str  # canonical heat URL, the join key against the inventory
    include: bool
    title: str
    kind: Literal["report", "rule", "publication"] | None = None
    reason: str = ""
    model: str = CURATION_MODEL
    curated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def candidate_url_key(candidate: Candidate) -> str:
    return normalize_url(candidate.heat_url)


def read_curation_store(path: Path = CURATION_STORE) -> dict[str, CurationRecord]:
    if not path.exists():
        return {}
    records: dict[str, CurationRecord] = {}
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                record = CurationRecord.model_validate_json(line)
                records[record.url] = record  # last write wins
    return records


def append_curation(path: Path, records: list[CurationRecord]) -> None:
    if not records:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for record in records:
            fh.write(record.model_dump_json() + "\n")


def llm_available() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY"))


def _client():
    import anthropic

    return anthropic.Anthropic()


# ---------------------------------------------------------------------------
# Stage 1: inventory triage. Batches of documents go to the model with a
# strict include/exclude policy; results are appended to the overlay store.
# ---------------------------------------------------------------------------

_TRIAGE_SYSTEM = """You curate the inventory of an NYC government report attention tracker. \
The dashboard ranks NYC city and state government documents by how much public \
attention each one draws. You review documents scraped from agency sites and \
decide, for each one, whether it belongs on the board and what its display \
title should be.

INCLUDE substantive government documents the public or press might discuss: \
reports, audits, investigations, studies, data briefs, plans, strategies, \
policy analyses, budget documents, testimony, rules (proposed or adopted), \
mandated recurring reports (quarterly/annual filings requried by local law), \
and similar publications.

EXCLUDE items that are not documents worth tracking on their own:
- non-English translations of a document (the English version is tracked separately)
- blank forms, applications, checklists, instructions for filling out forms
- brochures, flyers, posters, palm cards, outreach material
- press releases, newsletters, event or hearing notices, meeting agendas
- navigation pages, section indexes, service or program pages
- raw data files or appendix tables published apart from any narrative document

TITLE: write the document's proper display title.
- Use real title case and plain words; expand what the filename or slug clearly encodes.
- Never leave filename artifacts (underscores, dates stuck together, codes like "084Sum").
- For recurring mandated reports, name the series and period, e.g. \
"Local Law 26 Homeless Shelter Report, FY2024 Q1".
- Use the source agency and URL path as context. Do not invent specifics the \
input does not support; when the input only supports a generic description, \
write the best plain-language descriptive title you can.
- Keep titles under 120 characters.

KIND: "rule" only for rulemaking documents (proposed/adopted/emergency rules); \
otherwise "report".

Return a verdict for every item you are given, in the same order, using each \
item's index."""

_TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "include": {"type": "boolean"},
                    "title": {"type": "string"},
                    "kind": {"type": "string", "enum": ["report", "rule"]},
                    "reason": {"type": "string"},
                },
                "required": ["index", "include", "title", "kind", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


def _triage_payload(candidates: list[Candidate], mention_titles: dict[str, list[str]]) -> str:
    items = []
    for idx, candidate in enumerate(candidates):
        item = {
            "index": idx,
            "source": candidate.source_name,
            "kind": candidate.kind,
            "scraped_title": candidate.title,
            "url": candidate.heat_url,
            "published_date": str(candidate.published_date or ""),
        }
        shares = mention_titles.get(candidate_url_key(candidate), [])
        if shares:
            item["sample_share_text"] = shares[:3]
        items.append(item)
    return json.dumps(items, ensure_ascii=False)


def _default_triage_complete(payload: str, model: str) -> dict:
    response = _client().messages.create(
        model=model,
        max_tokens=8000,
        system=[{"type": "text", "text": _TRIAGE_SYSTEM, "cache_control": {"type": "ephemeral"}}],
        output_config={"format": {"type": "json_schema", "schema": _TRIAGE_SCHEMA}},
        messages=[{"role": "user", "content": payload}],
    )
    text = next(block.text for block in response.content if block.type == "text")
    return json.loads(text)


def curate_candidates(
    candidates: list[Candidate],
    store_path: Path = CURATION_STORE,
    mentions: list[HarvestedMention] | None = None,
    model: str = CURATION_MODEL,
    max_items: int = MAX_CURATED_PER_RUN,
    complete: Callable[[str, str], dict] | None = None,
) -> list[CurationRecord]:
    """Triage every candidate that has no curation record yet. Returns the new
    records (already appended to the store). Skips cleanly without an API key."""
    store = read_curation_store(store_path)
    pending = [c for c in candidates if candidate_url_key(c) not in store][:max_items]
    if not pending:
        return []
    if complete is None:
        if not llm_available():
            return []
        complete = _default_triage_complete

    # Share text from the mention store gives the model context a cryptic
    # filename lacks ("wow, the comptroller's storefront vacancy audit...").
    mention_titles: dict[str, list[str]] = {}
    for mention in mentions or []:
        title = (mention.title or "").strip()
        if title:
            mention_titles.setdefault(normalize_url(mention.target_url), []).append(title)

    new_records: list[CurationRecord] = []
    for start in range(0, len(pending), TRIAGE_BATCH_SIZE):
        batch = pending[start : start + TRIAGE_BATCH_SIZE]
        try:
            result = complete(_triage_payload(batch, mention_titles), model)
        except Exception as exc:
            # API failure (rate limit, billing, outage): stop here and let the
            # backlog drain on a later run, but say so instead of going quiet.
            print(f"warn: curation triage stopped: {exc}", file=sys.stderr)
            break
        for verdict in result.get("items", []):
            idx = verdict.get("index")
            if not isinstance(idx, int) or not (0 <= idx < len(batch)):
                continue
            candidate = batch[idx]
            new_records.append(
                CurationRecord(
                    url=candidate_url_key(candidate),
                    include=bool(verdict.get("include")),
                    title=(verdict.get("title") or candidate.title).strip()[:200],
                    kind=verdict.get("kind") if verdict.get("kind") in {"report", "rule"} else candidate.kind,
                    reason=(verdict.get("reason") or "")[:300],
                    model=model,
                )
            )
    append_curation(store_path, new_records)
    return new_records


def apply_curation_to_ranked(
    ranked: list[RankedItem],
    store: dict[str, CurationRecord],
) -> tuple[list[RankedItem], int]:
    """Curated view of the ranked list: excluded documents drop out, curated
    display titles and kinds replace the scraped ones. Uncurated items pass
    through unchanged (they get triaged on a later run). Matching and the
    untracked summary run on the full inventory before this filter, so an
    excluded document never resurfaces in the bubbling-under list."""
    kept: list[RankedItem] = []
    excluded = 0
    for item in ranked:
        record = store.get(candidate_url_key(item.candidate))
        if record is None:
            kept.append(item)
            continue
        if not record.include:
            excluded += 1
            continue
        item.candidate.title = record.title
        if record.kind:
            item.candidate.kind = record.kind
        kept.append(item)
    return kept, excluded


def apply_curation_to_candidates(
    candidates: list[Candidate],
    store: dict[str, CurationRecord],
) -> list[Candidate]:
    """Same overlay for plain candidate lists (the daily-delta view)."""
    kept: list[Candidate] = []
    for candidate in candidates:
        record = store.get(candidate_url_key(candidate))
        if record is None:
            kept.append(candidate)
            continue
        if not record.include:
            continue
        candidate.title = record.title
        if record.kind:
            candidate.kind = record.kind
        kept.append(candidate)
    return kept


# ---------------------------------------------------------------------------
# Stage 2: article-to-report matching. TV and tabloid coverage names reports
# without linking them, and rarely with the verbatim scraped title, so the
# regex tier misses it. Articles that pass a cheap keyword prefilter are read
# by the model against a shortlist of tracked documents.
# ---------------------------------------------------------------------------

_MATCH_SYSTEM = """You read a New York City news article and decide which tracked NYC \
government documents, if any, the article substantively covers.

You are given a list of tracked documents (URL, title, source, date) and the \
article text. Match a document ONLY when the article clearly refers to that \
specific document: it names the report or rule, attributes findings to it, or \
describes its release. Topic overlap is NOT a match; an article about housing \
does not match every housing report. When unsure, do not match.

For each match, copy the exact sentence from the article that names or \
identifies the document. If no tracked document is specifically covered, \
return an empty list."""

_MATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "matches": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "quote": {"type": "string"},
                },
                "required": ["url", "quote"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["matches"],
    "additionalProperties": False,
}

_REPORT_WORDS = re.compile(r"\b(report|audit|study|analysis|brief|investigation|findings|rule)\b", re.I)

_GENERIC_SOURCE_WORDS = {
    "nyc", "nys", "new", "york", "city", "state", "of", "the", "for", "and",
    "department", "office", "mayor's", "administration", "gov",
}


def build_agency_pattern(candidates: list[Candidate]) -> re.Pattern:
    """One regex of distinctive agency words ("comptroller", "ccrb", ...) used
    to prefilter articles before spending model tokens on them."""
    words: set[str] = set()
    for candidate in candidates:
        for word in re.split(r"[^\w']+", candidate.source_name.lower()):
            if word and word not in _GENERIC_SOURCE_WORDS and len(word) > 2:
                words.add(word)
    if not words:
        return re.compile(r"(?!)")
    return re.compile(r"\b(?:" + "|".join(re.escape(w) for w in sorted(words)) + r")\b", re.I)


def article_prefilter(text: str, agency_pattern: re.Pattern) -> bool:
    return bool(_REPORT_WORDS.search(text)) and bool(agency_pattern.search(text))


def _shortlist(candidates: list[Candidate], store: dict[str, CurationRecord]) -> list[Candidate]:
    """Documents worth matching against: curated-in (or not yet curated),
    newest first, capped so the prompt stays small and cacheable."""
    eligible = []
    for candidate in candidates:
        record = store.get(candidate_url_key(candidate))
        if record is not None and not record.include:
            continue
        eligible.append(candidate)
    eligible.sort(key=lambda c: str(c.published_date or "0000"), reverse=True)
    return eligible[:SHORTLIST_LIMIT]


def _shortlist_block(shortlist: list[Candidate], store: dict[str, CurationRecord]) -> str:
    lines = []
    for candidate in shortlist:
        record = store.get(candidate_url_key(candidate))
        title = record.title if record else candidate.title
        lines.append(
            f"{candidate_url_key(candidate)} | {title} | {candidate.source_name} | {candidate.published_date or 'undated'}"
        )
    return "\n".join(lines)


def _default_match_complete(system_blocks: list[dict], article_payload: str, model: str) -> dict:
    response = _client().messages.create(
        model=model,
        max_tokens=2000,
        system=system_blocks,
        output_config={"format": {"type": "json_schema", "schema": _MATCH_SCHEMA}},
        messages=[{"role": "user", "content": article_payload}],
    )
    text = next(block.text for block in response.content if block.type == "text")
    return json.loads(text)


def make_article_matcher(
    candidates: list[Candidate],
    store_path: Path = CURATION_STORE,
    model: str = CURATION_MODEL,
    max_articles: int = MAX_LLM_ARTICLES_PER_RUN,
    complete: Callable[[list[dict], str, str], dict] | None = None,
) -> Callable[[dict, str, set[str]], list[HarvestedMention]] | None:
    """Returns matcher(article, text, already_matched_targets) -> mentions, or
    None when no API key is available. The tracked-document shortlist lives in
    a cached system block so per-article cost is mostly just the article text."""
    if complete is None:
        if not llm_available():
            return None
        complete = _default_match_complete

    store = read_curation_store(store_path)
    shortlist = _shortlist(candidates, store)
    if not shortlist:
        return None
    valid_targets = {candidate_url_key(c) for c in shortlist}
    agency_pattern = build_agency_pattern(shortlist)
    system_blocks = [
        {"type": "text", "text": _MATCH_SYSTEM},
        {
            "type": "text",
            "text": "Tracked documents:\n" + _shortlist_block(shortlist, store),
            "cache_control": {"type": "ephemeral"},
        },
    ]
    budget = {"remaining": max_articles}

    def matcher(article: dict, text: str, already_matched: set[str]) -> list[HarvestedMention]:
        if budget["remaining"] <= 0:
            return []
        clean = text[:ARTICLE_TEXT_LIMIT]
        if not article_prefilter(clean, agency_pattern):
            return []
        budget["remaining"] -= 1
        payload = json.dumps(
            {"headline": article.get("title"), "outlet": article.get("outlet"), "text": clean},
            ensure_ascii=False,
        )
        try:
            result = complete(system_blocks, payload, model)
        except Exception as exc:
            print(f"warn: LLM article matching stopped: {exc}", file=sys.stderr)
            budget["remaining"] = 0  # API trouble: stop spending for this run
            return []
        mentions = []
        for match in result.get("matches", []):
            target = normalize_url(str(match.get("url", "")))
            # already_matched holds www-folded targets (harvest's convention)
            if target not in valid_targets or target.replace("://www.", "://", 1) in already_matched:
                continue
            mentions.append(
                HarvestedMention(
                    uid=f"newsrss:llmnamed:{article['key']}:{target}",
                    provider="newsrss",
                    query=article.get("feed_url", ""),
                    target_url=target,
                    source_url=article.get("url"),
                    title=article.get("title"),
                    author=article.get("outlet"),
                    published_at=article.get("published_at"),
                    via="title",
                    match_quote=(match.get("quote") or "")[:500],
                )
            )
        return mentions

    return matcher
