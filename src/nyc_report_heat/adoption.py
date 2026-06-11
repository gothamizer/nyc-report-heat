"""Adoption: discovery from the mention store.

Listing scrapers find documents going forward; adoption closes the other gap —
a document (often published long ago) that people are sharing *now*. Any
unmatched report-like gov link in the mention store is promoted into the
tracked inventory, attributed to its source when the host is one we know, so
nothing useful is left sitting in the untracked list.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from nyc_report_heat.dates import extract_date
from nyc_report_heat.discovery import AGENCY_SOURCES, SOURCE_NAMES, _filename_title, _id_for
from nyc_report_heat.harvest import build_candidate_index, match_mention
from nyc_report_heat.http import HttpClient
from nyc_report_heat.io import report_like_url
from nyc_report_heat.models import Candidate, HarvestedMention
from nyc_report_heat.urltools import detect_format, filename_from_url, normalize_url

# Known hosts -> (source_id prefix, source name). nyc.gov paths are attributed
# through AGENCY_SOURCES url_filters below; anything else keeps its host as
# the source so attribution stays honest.
HOST_SOURCES: dict[str, tuple[str, str]] = {
    "comptroller.nyc.gov": ("nyc_comptroller", SOURCE_NAMES["nyc_comptroller"]),
    "osc.ny.gov": ("nys_comptroller", SOURCE_NAMES["nys_comptroller"]),
    "ibo.nyc.ny.us": ("ibo", SOURCE_NAMES["ibo"]),
    "council.nyc.gov": ("council", SOURCE_NAMES["council"]),
    "a860-gpp.nyc.gov": ("gpp", SOURCE_NAMES["gpp"]),
    "advocate.nyc.gov": ("pubadvocate", "NYC Public Advocate"),
    "pubadvocate.nyc.gov": ("pubadvocate", "NYC Public Advocate"),
    "criminaljustice.cityofnewyork.us": ("mocj", "NYC Mayor's Office of Criminal Justice"),
}

_NYC_GOV_PATH_SOURCES: list[tuple[str, str, str]] = [
    ("/assets/doi/", "doi", SOURCE_NAMES["doi"]),
    ("/assets/omb/", "omb", "NYC Office of Management & Budget"),
    ("/assets/planning/", "dcp", "NYC Department of City Planning"),
    ("/assets/operations/", "mmr", "NYC Mayor's Office of Operations"),
] + [
    (source["url_filter"].lower(), source["id"], source["name"])
    for source in AGENCY_SOURCES
    if source["url_filter"].startswith(("/assets/", "/html/"))
]


def _source_for(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    host = parsed.netloc.removeprefix("www.")
    if host in HOST_SOURCES:
        return HOST_SOURCES[host]
    if host in {"nyc.gov", "home4.nyc.gov"}:
        path = parsed.path.lower()
        for prefix, source_id, name in _NYC_GOV_PATH_SOURCES:
            if prefix in path:
                return source_id, name
    return host, host


def _slug_title(url: str) -> str:
    segment = [seg for seg in urlparse(url).path.split("/") if seg][-1]
    segment = re.sub(r"\.(page|html?|shtml|aspx?)$", "", segment, flags=re.I)
    return re.sub(r"[_\-]+", " ", segment).strip().title()


def _page_title(client: HttpClient | None, url: str) -> str | None:
    if client is None:
        return None
    try:
        soup = client.soup(url)
    except Exception:
        return None
    og = soup.find("meta", property="og:title")
    if og and og.get("content", "").strip():
        return og["content"].strip()
    if soup.title and soup.title.get_text(strip=True):
        return soup.title.get_text(strip=True)
    return None


def adopt_from_mentions(
    mentions: list[HarvestedMention],
    candidates: list[Candidate],
    client: HttpClient | None = None,
) -> list[Candidate]:
    """Candidates for every unmatched report-like gov link in the mention
    store. HTML titles come from the page itself (when a client is given);
    document titles fall back to the cleaned filename or URL slug."""
    by_url, by_filename = build_candidate_index(candidates)
    adopted: list[Candidate] = []
    seen: set[str] = set()
    for mention in mentions:
        key, _ = match_mention(mention, by_url, by_filename)
        if key is not None:
            continue
        canonical = normalize_url(mention.target_url)
        if not report_like_url(canonical):
            continue
        folded = canonical.replace("://www.", "://", 1)
        if folded in seen:
            continue
        seen.add(folded)
        source_id, source_name = _source_for(canonical)
        fmt = detect_format(canonical)
        if fmt in {"pdf", "docx", "xlsx"}:
            title = _filename_title(canonical)
            if len(title) < 8:  # cryptic filename ("04.pdf"): add path context
                segments = [
                    seg for seg in urlparse(canonical).path.split("/")
                    if seg and seg.lower() not in {"assets", "downloads", "pdf", "html"}
                ][-3:]
                segments[-1] = re.sub(r"\.(pdf|docx?|xlsx?|csv)$", "", segments[-1], flags=re.I)
                title = re.sub(r"[_\-]+", " ", " ".join(segments)).strip().title()
        else:
            title = _page_title(client, canonical) or _slug_title(canonical)
        kind = "rule" if urlparse(canonical).netloc.endswith("rules.cityofnewyork.us") else "report"
        adopted.append(
            Candidate(
                source_id=_id_for(source_id, canonical),
                source_name=source_name,
                kind=kind,  # type: ignore[arg-type]
                title=(title or canonical)[:300],
                url=canonical,
                document_url=canonical if fmt in {"pdf", "docx", "xlsx"} else None,
                published_date=extract_date(filename_from_url(canonical) or ""),
                summary="Adopted from public pickups of this link.",
                format=fmt if fmt != "unknown" else "html",  # type: ignore[arg-type]
                source_page=None,
            )
        )
    return adopted
