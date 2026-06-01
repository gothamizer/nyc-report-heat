from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from datetime import date, timedelta

from bs4 import BeautifulSoup, Tag

from nyc_report_heat.dates import extract_date
from nyc_report_heat.http import HttpClient
from nyc_report_heat.models import Candidate
from nyc_report_heat.urltools import absolutize, detect_format, normalize_url


SOURCE_PAGES = {
    "doi": "https://www.nyc.gov/site/doi/newsroom/public-reports-current.page",
    "nyc_comptroller": "https://comptroller.nyc.gov/reports/",
    "nys_comptroller": "https://www.osc.ny.gov/reports",
    "ibo": "https://ibo.nyc.ny.us/publications.html",
    "rules_proposed": "https://rules.cityofnewyork.us/proposed-rules/",
    "rules_adopted": "https://rules.cityofnewyork.us/recently-adopted-rules/",
    "gpp": "https://a860-gpp.nyc.gov",
}


SOURCE_NAMES = {
    "doi": "NYC Department of Investigation",
    "nyc_comptroller": "NYC Comptroller",
    "nys_comptroller": "NYS Comptroller",
    "ibo": "NYC Independent Budget Office",
    "rules_proposed": "NYC Rules - Proposed",
    "rules_adopted": "NYC Rules - Adopted",
    "gpp": "NYC Government Publications Portal",
}


REPORT_WORDS = re.compile(r"\b(report|audit|analysis|review|investigation|brief|publication|study|rule|notice)\b", re.I)
DATE_WORDS = re.compile(
    r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}\b|\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b|\b\d{4}-\d{2}-\d{2}\b",
    re.I,
)
NAV_TITLES = {
    "311",
    "search",
    "search all nyc.gov websites",
    "menu",
    "text-size",
    "home",
    "about",
    "contact",
    "services",
    "events",
    "your government",
    "privacy policy",
    "terms of use",
    "careers",
    "subscribe",
    "translate",
    "stay connected",
}


def _id_for(source_id: str, url: str) -> str:
    return f"{source_id}:{hashlib.sha1(normalize_url(url).encode()).hexdigest()[:12]}"


def _text(node: Tag) -> str:
    return " ".join(node.get_text(" ", strip=True).split())


def _candidate_from_anchor(
    source_id: str,
    source_name: str,
    kind: str,
    anchor: Tag,
    base_url: str,
    source_page: str,
    agency: str | None = None,
) -> Candidate | None:
    href = anchor.get("href")
    title = _text(anchor)
    if not href or not title or title.strip().lower() in NAV_TITLES or href.startswith(("mailto:", "tel:", "#", "javascript:")):
        return None
    url = normalize_url(absolutize(href, base_url))
    if not url.startswith("http"):
        return None
    surrounding = _text(anchor.parent) if isinstance(anchor.parent, Tag) else title
    published = extract_date(surrounding)
    fmt = detect_format(url)
    if fmt == "unknown" and not REPORT_WORDS.search(title + " " + surrounding):
        return None
    return Candidate(
        source_id=_id_for(source_id, url),
        source_name=source_name,
        kind=kind,  # type: ignore[arg-type]
        title=title,
        agency=agency,
        url=url,
        document_url=url if fmt in {"pdf", "docx", "xlsx"} else None,
        published_date=published,
        summary=surrounding if surrounding != title else None,
        format=fmt,  # type: ignore[arg-type]
        source_page=source_page,
    )


def _dedupe(candidates: Iterable[Candidate]) -> list[Candidate]:
    seen: dict[str, Candidate] = {}
    for candidate in candidates:
        key = normalize_url(candidate.heat_url)
        existing = seen.get(key)
        if not existing:
            seen[key] = candidate
            continue
        if not existing.published_date and candidate.published_date:
            seen[key] = candidate
    return list(seen.values())


def _limit_reached(candidates: list[Candidate], limit: int | None) -> bool:
    return limit is not None and limit > 0 and len(candidates) >= limit


def _in_lookback(candidate: Candidate, discovered_after: date | None) -> bool:
    if discovered_after is None or candidate.published_date is None:
        return True
    return candidate.published_date >= discovered_after


def _append_candidate(candidates: list[Candidate], candidate: Candidate, seen: set[str], discovered_after: date | None) -> None:
    key = normalize_url(candidate.heat_url)
    if key in seen or not _in_lookback(candidate, discovered_after):
        return
    seen.add(key)
    candidates.append(candidate)


def lookback_date(days: int | None) -> date | None:
    if not days or days <= 0:
        return None
    return date.today() - timedelta(days=days)


def discover_generic_source(
    client: HttpClient,
    source_id: str,
    limit: int = 100,
    discovered_after: date | None = None,
) -> list[Candidate]:
    url = SOURCE_PAGES[source_id]
    soup = client.soup(url)
    source_name = SOURCE_NAMES[source_id]
    kind = "rule" if source_id.startswith("rules") else "report"
    candidates: list[Candidate] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a"):
        if _limit_reached(candidates, limit):
            break
        candidate = _candidate_from_anchor(source_id, source_name, kind, anchor, url, url)
        if candidate:
            _append_candidate(candidates, candidate, seen, discovered_after)
    return candidates


def discover_doi(client: HttpClient, limit: int = 100, discovered_after: date | None = None) -> list[Candidate]:
    pages = [
        SOURCE_PAGES["doi"],
        "https://www.nyc.gov/site/doi/newsroom/public-reports-2022-2025.page",
    ]
    candidates: list[Candidate] = []
    seen: set[str] = set()
    for page in pages:
        soup = client.soup(page)
        for anchor in soup.find_all("a", href=True):
            if _limit_reached(candidates, limit):
                break
            href = anchor["href"]
            if "/assets/doi/" not in href and "/assets/doi/" not in absolutize(href, page):
                continue
            candidate = _candidate_from_anchor("doi", SOURCE_NAMES["doi"], "report", anchor, page, page)
            if candidate:
                previous_date = anchor.find_previous("strong")
                if previous_date:
                    candidate.published_date = extract_date(_text(previous_date))
                _append_candidate(candidates, candidate, seen, discovered_after)
    return candidates


def discover_nyc_comptroller(client: HttpClient, limit: int = 100, discovered_after: date | None = None) -> list[Candidate]:
    page = SOURCE_PAGES["nyc_comptroller"]
    soup = client.soup(page)
    main = soup.select_one("main") or soup
    candidates: list[Candidate] = []
    seen: set[str] = set()
    for anchor in main.find_all("a", href=True):
        if _limit_reached(candidates, limit):
            break
        href = absolutize(anchor["href"], page)
        if "comptroller.nyc.gov/reports/" not in href:
            continue
        if href.rstrip("/") in {"https://comptroller.nyc.gov/reports", "https://comptroller.nyc.gov/reports/"}:
            continue
        surrounding = _text(anchor.parent) if isinstance(anchor.parent, Tag) else _text(anchor)
        if not DATE_WORDS.search(surrounding):
            continue
        candidate = _candidate_from_anchor("nyc_comptroller", SOURCE_NAMES["nyc_comptroller"], "report", anchor, page, page)
        if candidate:
            candidate.title = re.sub(r"^(?:[A-Z][a-z]{2}\s+\d{1,2},\s+\d{4}\s+)", "", candidate.title).strip()
            candidate.title = DATE_WORDS.sub("", candidate.title).strip()
            if not _in_lookback(candidate, discovered_after):
                continue
            key = normalize_url(candidate.heat_url)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(candidate)
    return candidates


def discover_nys_comptroller(client: HttpClient, limit: int = 100, discovered_after: date | None = None) -> list[Candidate]:
    page = SOURCE_PAGES["nys_comptroller"]
    soup = client.soup(page)
    content = soup.select_one(".view-content") or soup
    candidates: list[Candidate] = []
    seen: set[str] = set()
    for anchor in content.find_all("a", href=True):
        if _limit_reached(candidates, limit):
            break
        href = absolutize(anchor["href"], page)
        title = _text(anchor)
        if len(title) < 12 or title.lower().startswith(("page ", "current page", "next page", "last page", "regional table")):
            continue
        if "/reports" not in href and "/files/reports" not in href and "/files/local-government/publications" not in href:
            continue
        candidate = _candidate_from_anchor("nys_comptroller", SOURCE_NAMES["nys_comptroller"], "report", anchor, page, page)
        if candidate:
            _append_candidate(candidates, candidate, seen, discovered_after)
    return candidates


def discover_ibo(client: HttpClient, limit: int = 100, discovered_after: date | None = None) -> list[Candidate]:
    pages = [
        "https://ibo.nyc.ny.us/publicationsAnnuals.html",
        "https://ibo.nyc.ny.us/publicationsSocialCommunity.html",
        "https://ibo.nyc.ny.us/publicationsBudgetProcess.html",
        "https://ibo.nyc.ny.us/publicationsEICB.html",
        "https://ibo.nyc.ny.us/publicationsEducation.html",
        "https://ibo.nyc.ny.us/publicationsTaxRev.html",
    ]
    candidates: list[Candidate] = []
    seen: set[str] = set()
    for page in pages:
        if _limit_reached(candidates, limit):
            break
        soup = client.soup(page)
        for anchor in soup.find_all("a", href=True):
            if _limit_reached(candidates, limit):
                break
            href = absolutize(anchor["href"], page)
            title = _text(anchor)
            if len(title) < 12 or title.upper() in {"PDF", "HTML"}:
                continue
            if "ibo.nyc.ny.us" not in href or not ("/iboreports/" in href or "/cgi-park" in href):
                continue
            candidate = _candidate_from_anchor("ibo", SOURCE_NAMES["ibo"], "report", anchor, page, page)
            if candidate:
                _append_candidate(candidates, candidate, seen, discovered_after)
    return candidates


def discover_rules(
    client: HttpClient,
    source_id: str,
    limit: int = 100,
    discovered_after: date | None = None,
) -> list[Candidate]:
    url = SOURCE_PAGES[source_id]
    soup = client.soup(url)
    candidates: list[Candidate] = []
    seen: set[str] = set()
    for row in soup.find_all("tr"):
        if _limit_reached(candidates, limit):
            break
        cells = row.find_all(["td", "th"])
        if len(cells) < 2:
            continue
        anchor = row.find("a")
        if not anchor:
            continue
        candidate = _candidate_from_anchor(source_id, SOURCE_NAMES[source_id], "rule", anchor, url, url)
        if not candidate:
            continue
        candidate.agency = _text(cells[1]) if len(cells) > 1 else None
        candidate.published_date = extract_date(_text(row))
        candidate.summary = _text(row)
        _append_candidate(candidates, candidate, seen, discovered_after)
    if candidates:
        return candidates
    return discover_generic_source(client, source_id, limit, discovered_after)


def discover_gpp_recent(client: HttpClient, limit: int = 100, discovered_after: date | None = None) -> list[Candidate]:
    # The portal is a Blacklight app; the public search page is easier and more stable
    # for low-hundreds discovery than reverse-engineering every facet endpoint.
    url = "https://a860-gpp.nyc.gov/?locale=en"
    soup = client.soup(url)
    candidates: list[Candidate] = []
    seen: set[str] = set()
    for doc in soup.select(".document, article, tr"):
        if _limit_reached(candidates, limit):
            break
        anchor = doc.find("a", href=True)
        if not anchor:
            continue
        title = _text(anchor)
        if not title:
            continue
        item_url = normalize_url(absolutize(anchor["href"], url))
        text = _text(doc)
        agency = None
        agency_match = re.search(r"(?:Agency|Publisher)\s*[:\-]?\s*([A-Z][A-Za-z&,\s]+)", text)
        if agency_match:
            agency = agency_match.group(1).strip()
        candidate = Candidate(
            source_id=_id_for("gpp", item_url),
            source_name=SOURCE_NAMES["gpp"],
            kind="publication",
            title=title,
            agency=agency,
            url=item_url,
            document_url=None,
            published_date=extract_date(text),
            summary=text,
            format="html",
            source_page=url,
        )
        _append_candidate(candidates, candidate, seen, discovered_after)
    return candidates


def discover_all(
    client: HttpClient | None = None,
    per_source: int = 50,
    source_ids: list[str] | None = None,
    discovered_after: date | None = None,
) -> list[Candidate]:
    client = client or HttpClient()
    all_candidates: list[Candidate] = []
    source_funcs = {
        "doi": discover_doi,
        "nyc_comptroller": discover_nyc_comptroller,
        "nys_comptroller": discover_nys_comptroller,
        "ibo": discover_ibo,
    }
    wanted = source_ids or [
        "doi",
        "nyc_comptroller",
        "nys_comptroller",
        "ibo",
        "rules_proposed",
        "rules_adopted",
        "gpp",
    ]
    for source_id in wanted:
        if source_id not in source_funcs:
            continue
        func = source_funcs[source_id]
        try:
            all_candidates.extend(func(client, per_source, discovered_after))
        except Exception as exc:
            print(f"warning: failed discovery for {source_id}: {exc}")
    for source_id in ("rules_proposed", "rules_adopted"):
        if source_id not in wanted:
            continue
        try:
            all_candidates.extend(discover_rules(client, source_id, per_source, discovered_after))
        except Exception as exc:
            print(f"warning: failed discovery for {source_id}: {exc}")
    if "gpp" in wanted:
        try:
            all_candidates.extend(discover_gpp_recent(client, per_source, discovered_after))
        except Exception as exc:
            print(f"warning: failed discovery for gpp: {exc}")
    return _dedupe(all_candidates)
