from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from datetime import date, timedelta

from bs4 import BeautifulSoup, Tag

from nyc_report_heat.dates import extract_date
from nyc_report_heat.http import HttpClient
from nyc_report_heat.models import Candidate
from nyc_report_heat.urltools import absolutize, detect_format, filename_from_url, normalize_url


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
    "skip to main content",
    "skip to content",
    "back to top",
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


# Config-driven agency/oversight-body report scrapers. Every entry was verified
# to serve static HTML whose report links carry a distinguishing url_filter
# substring. `limit` caps deep archive pages so the board stays recent-weighted.
AGENCY_SOURCES: list[dict] = [
    # — law enforcement, corrections & oversight —
    {"id": "ccrb", "name": "NYC Civilian Complaint Review Board", "kind": "report",
     "url_filter": "/assets/ccrb/downloads/", "limit": 40,
     "pages": ["https://www.nyc.gov/site/ccrb/policy/annual-bi-annual-reports.page",
               "https://www.nyc.gov/site/ccrb/policy/issue-based-reports.page"]},
    {"id": "doc", "name": "NYC Department of Correction", "kind": "report",
     "url_filter": "/assets/doc/downloads/", "limit": 40,
     "pages": ["https://www.nyc.gov/site/doc/data/statistics-and-compliance.page"]},
    {"id": "boc", "name": "NYC Board of Correction", "kind": "report",
     "url_filter": "/assets/boc/downloads/", "limit": 40,
     "pages": ["https://www.nyc.gov/site/boc/reports/board-of-correction-reports.page"]},
    {"id": "nypd", "name": "NYPD", "kind": "report",
     "url_filter": "/assets/nypd/downloads/", "limit": 50,
     "pages": ["https://www.nyc.gov/site/nypd/stats/reports-analysis/use-of-force.page",
               "https://www.nyc.gov/site/nypd/stats/reports-analysis/stopfrisk.page",
               "https://www.nyc.gov/site/nypd/stats/reports-analysis/firearms-discharge.page"]},
    {"id": "coib", "name": "NYC Conflicts of Interest Board", "kind": "report",
     "url_filter": "/assets/coib/downloads/", "limit": 30,
     "pages": ["https://www.nyc.gov/site/coib/public-documents/annual-reports.page",
               "https://www.nyc.gov/site/coib/public-documents/public-documents.page"]},
    {"id": "mocj", "name": "NYC Mayor's Office of Criminal Justice", "kind": "report",
     "url_filter": "/wp-content/uploads/", "limit": 40,
     "pages": ["https://criminaljustice.cityofnewyork.us/briefs/"]},
    {"id": "law", "name": "NYC Law Department", "kind": "report",
     "url_filter": "/assets/law/downloads/", "limit": 20,
     "pages": ["https://www.nyc.gov/site/law/news/annual-reports-archives.page"]},
    # — housing, buildings, land use, environment —
    {"id": "hpd", "name": "NYC Housing Preservation & Development", "kind": "report",
     "url_filter": "/assets/hpd/", "limit": 50,
     "pages": ["https://www.nyc.gov/site/hpd/about/research.page"]},
    {"id": "nycha", "name": "NYC Housing Authority", "kind": "report",
     "url_filter": "/assets/nycha/", "limit": 50,
     "pages": ["https://www.nyc.gov/site/nycha/about/reports.page"]},
    {"id": "dob", "name": "NYC Department of Buildings", "kind": "report",
     "url_filter": "/assets/buildings/html/", "limit": 30,
     "pages": ["https://www.nyc.gov/site/buildings/dob/analytics-reports.page"]},
    {"id": "lpc", "name": "NYC Landmarks Preservation Commission", "kind": "report",
     "url_filter": "/assets/lpc/", "limit": 25,
     "pages": ["https://www.nyc.gov/site/lpc/about/archaeology-reports-full-list.page"]},
    {"id": "dep", "name": "NYC Department of Environmental Protection", "kind": "report",
     "url_filter": "/assets/dep/downloads/pdf/", "limit": 40,
     "pages": ["https://www.nyc.gov/site/dep/about/drinking-water-supply-quality-report.page",
               "https://www.nyc.gov/site/dep/water/harbor-water-quality.page"]},
    {"id": "dsny", "name": "NYC Department of Sanitation", "kind": "report",
     "url_filter": "/assets/dsny/downloads/", "limit": 30,
     "pages": ["https://www.nyc.gov/site/dsny/resources/reports/archive.page"]},
    # — health & human services —
    {"id": "dohmh", "name": "NYC Department of Health & Mental Hygiene", "kind": "report",
     "url_filter": "/assets/doh/downloads/pdf/", "limit": 50,
     "pages": ["https://www.nyc.gov/site/doh/data/data-publications/epi-data-briefs-and-data-tables.page",
               "https://www.nyc.gov/site/doh/data/data-publications/epi-research-reports.page"]},
    {"id": "acs", "name": "NYC Administration for Children's Services", "kind": "report",
     "url_filter": "/assets/acs/pdf/", "limit": 50,
     "pages": ["https://www.nyc.gov/site/acs/about/reports-archive.page",
               "https://www.nyc.gov/site/acs/about/data-analysis.page"]},
    {"id": "dhs", "name": "NYC Department of Homeless Services", "kind": "report",
     "url_filter": "/assets/dhs/downloads/pdf/", "limit": 40,
     "pages": ["https://www.nyc.gov/site/dhs/about/stats-and-reports.page"]},
    {"id": "hra", "name": "NYC Human Resources Administration", "kind": "report",
     "url_filter": "/assets/hra/downloads/pdf/", "limit": 50,
     "pages": ["https://www.nyc.gov/site/hra/about/facts.page",
               "https://www.nyc.gov/site/hra/about/dss-research-corner.page"]},
    {"id": "dfta", "name": "NYC Department for the Aging", "kind": "report",
     "url_filter": "/assets/dfta/downloads/pdf/", "limit": 30,
     "pages": ["https://www.nyc.gov/site/dfta/news-reports/reports.page",
               "https://www.nyc.gov/site/dfta/news-reports/publications.page"]},
    {"id": "dycd", "name": "NYC Department of Youth & Community Development", "kind": "report",
     "url_filter": "/assets/dycd/downloads/pdf/", "limit": 20,
     "pages": ["https://www.nyc.gov/site/dycd/about/news-and-media/annual-report.page"]},
    # — economy, infrastructure, services & citywide oversight —
    {"id": "dot", "name": "NYC Department of Transportation", "kind": "report",
     "url_filter": "/html/dot/downloads/", "limit": 50,
     "pages": ["https://www.nyc.gov/html/dot/html/about/dotlibrary.shtml"]},
    {"id": "dcwp", "name": "NYC Department of Consumer & Worker Protection", "kind": "report",
     "url_filter": "/assets/dca/", "limit": 50,
     "pages": ["https://www.nyc.gov/site/dca/media/research.page"]},
    {"id": "tlc", "name": "NYC Taxi & Limousine Commission", "kind": "report",
     "url_filter": "/assets/tlc/downloads/", "limit": 40,
     "pages": ["https://www.nyc.gov/site/tlc/about/industry-reports.page"]},
    {"id": "sbs", "name": "NYC Department of Small Business Services", "kind": "report",
     "url_filter": "/assets/sbs/downloads/", "limit": 40,
     "pages": ["https://www.nyc.gov/site/sbs/about/publications-reports.page"]},
    {"id": "dof", "name": "NYC Department of Finance", "kind": "report",
     "url_filter": "/assets/finance/downloads/", "limit": 40,
     "pages": ["https://www.nyc.gov/site/finance/property/property-reports.page"]},
    {"id": "pubadvocate", "name": "NYC Public Advocate", "kind": "report",
     "url_filter": "/reports/", "limit": 40,
     "pages": ["https://www.pubadvocate.nyc.gov/reports/"]},
    {"id": "mmr", "name": "NYC Mayor's Management Report", "kind": "report",
     "url_filter": "/assets/operations/downloads/", "limit": 40,
     "pages": ["https://www.nyc.gov/site/operations/reports/mmr.page"]},
]

AGENCY_BY_ID = {source["id"]: source for source in AGENCY_SOURCES}


_TITLE_JUNK = {"", "pdf", "html", "download", "read more", "report", "link", "here", "view", "open", "more"}


def _agency_title(anchor: Tag, url: str) -> str:
    title = _text(anchor)
    if title.lower() in _TITLE_JUNK or len(title) < 6:
        name = filename_from_url(url) or ""
        name = re.sub(r"\.(pdf|docx?|xlsx?|csv)$", "", name, flags=re.I)
        name = re.sub(r"[_\-]+", " ", name)
        name = re.sub(r"%20", " ", name).strip()
        if name:
            title = name.title()
    return title


def discover_agency(
    client: HttpClient,
    source: dict,
    limit: int = 50,
    discovered_after: date | None = None,
) -> list[Candidate]:
    """Generic scraper for an agency report page: keep links whose URL carries
    the source's distinguishing filter substring. Title falls back to a cleaned
    filename when the link text is generic ("PDF", "Download")."""
    flt = source["url_filter"].lower()
    cap = min(source.get("limit", 50), limit) if limit else source.get("limit", 50)
    candidates: list[Candidate] = []
    seen: set[str] = set()
    for page in source["pages"]:
        if _limit_reached(candidates, cap):
            break
        try:
            soup = client.soup(page)
        except Exception as exc:
            print(f"warning: failed discovery for {source['id']} ({page}): {exc}")
            continue
        for anchor in soup.find_all("a", href=True):
            if _limit_reached(candidates, cap):
                break
            href = absolutize(anchor["href"], page)
            if flt not in href.lower():
                continue
            url = normalize_url(href)
            if not url.startswith("http"):
                continue
            title = _agency_title(anchor, url)
            if not title or title.strip().lower() in NAV_TITLES:
                continue
            fmt = detect_format(url)
            surrounding = _text(anchor.parent) if isinstance(anchor.parent, Tag) else title
            published = extract_date(surrounding) or extract_date(filename_from_url(url) or "")
            candidate = Candidate(
                source_id=_id_for(source["id"], url),
                source_name=source["name"],
                kind=source["kind"],  # type: ignore[arg-type]
                title=title[:300],
                url=url,
                document_url=url if fmt in {"pdf", "docx", "xlsx"} else None,
                published_date=published,
                summary=surrounding if surrounding != title else None,
                format=fmt,  # type: ignore[arg-type]
                source_page=page,
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
    for source_id in wanted:
        source = AGENCY_BY_ID.get(source_id)
        if source is None:
            continue
        try:
            all_candidates.extend(discover_agency(client, source, per_source, discovered_after))
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
