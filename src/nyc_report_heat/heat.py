from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests
from bs4 import BeautifulSoup

from nyc_report_heat.http import USER_AGENT, HttpClient
from nyc_report_heat.models import Candidate, HeatResult, Mention
from nyc_report_heat.urltools import canonical_variants, filename_from_url


def _parse_gdelt_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def query_gdelt(
    client: HttpClient,
    candidate: Candidate,
    days: int,
    max_records: int = 25,
    expanded_variants: bool = False,
) -> tuple[int, list[Mention], list[str]]:
    mentions: list[Mention] = []
    errors: list[str] = []
    queries = _heat_queries(candidate, expanded_variants=expanded_variants)
    for query, confidence in queries:
        params = {
            "query": f'"{query}"',
            "mode": "ArtList",
            "format": "json",
            "maxrecords": str(max_records),
            "timespan": f"{days}d",
        }
        try:
            data = client.get("https://api.gdeltproject.org/api/v2/doc/doc", params=params).json()
        except Exception as exc:
            errors.append(f"gdelt:{query}:{exc}")
            continue
        for article in data.get("articles", []):
            mentions.append(
                Mention(
                    provider="gdelt",
                    query=query,
                    url=article.get("url"),
                    title=article.get("title"),
                    published_at=_parse_gdelt_datetime(article.get("seendate")),
                    confidence=confidence,  # type: ignore[arg-type]
                )
            )
    unique = {(m.url, m.query) for m in mentions}
    return len(unique), mentions, errors


# Boilerplate that appears across many NYC report titles and carries no
# distinguishing signal; dropped before building the news query.
_TITLE_STOPWORDS = {
    "the", "a", "an", "of", "on", "in", "into", "to", "for", "and", "or", "by",
    "with", "at", "as", "re", "its", "from", "about", "under",
    "report", "reports", "investigation", "investigations", "review", "annual",
    "quarterly", "monthly", "update", "testimony", "letter", "rule", "rules",
    "proposed", "adopted", "repeal", "repealing", "amendment", "amended",
    "relating", "concerning", "regarding", "current", "implementation",
    "department", "office", "city", "new", "york", "nyc", "state",
}


def distinctive_query(title: str, max_terms: int = 5) -> str | None:
    """Build a GDELT news query from a report title.

    News rarely repeats a report's exact title, so an exact-phrase match is
    almost always empty. Instead we keep the title's distinctive content terms
    (dropping shared government boilerplate) and AND them together, which stays
    anchored to the specific report while actually matching real articles.
    """
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9\-']+", title)
    terms: list[str] = []
    for word in words:
        if word.lower() in _TITLE_STOPWORDS or len(word) < 3:
            continue
        if word not in terms:
            terms.append(word)
    # Prefer the most distinctive (longer / capitalised) terms.
    terms.sort(key=lambda w: (w[:1].isupper(), len(w)), reverse=True)
    terms = terms[:max_terms]
    if len(terms) < 2:
        return None
    return " ".join(terms)


def query_gdelt_coverage(
    client: HttpClient,
    candidate: Candidate,
    days: int,
    lookback_days: int = 30,
    max_records: int = 75,
) -> tuple[int, list[Mention], list[str]]:
    """Real news coverage of the report, matched on its distinctive title terms.

    GDELT is queried once at the widest lookback window; because the request
    params do not depend on `days`, the HttpClient response cache serves the
    same fetch for every heat window. Articles are then bucketed into the
    requested window client-side by their seen date.
    """
    mentions: list[Mention] = []
    errors: list[str] = []
    query = distinctive_query(candidate.title or "")
    if not query:
        return 0, [], []
    params = {
        "query": query,
        "mode": "ArtList",
        "format": "json",
        "maxrecords": str(max_records),
        "timespan": f"{lookback_days}d",
        "sort": "DateDesc",
    }
    try:
        data = client.get("https://api.gdeltproject.org/api/v2/doc/doc", params=params).json()
    except Exception as exc:
        errors.append(f"gdelt-coverage:{query[:48]}:{exc}")
        return 0, [], errors
    cutoff = _cutoff(days)
    # GDELT AND-matches the query terms anywhere in the article body, which can
    # surface coincidental hits. Require the headline to carry at least one
    # distinctive term so a counted article is plausibly about this report.
    terms = [t.lower() for t in query.split()]
    for article in data.get("articles", []):
        seen = _parse_gdelt_datetime(article.get("seendate"))
        if seen is not None and seen < cutoff:
            continue
        headline = (article.get("title") or "").lower()
        if not any(term in headline for term in terms):
            continue
        mentions.append(
            Mention(
                provider="gdelt",
                query=query,
                url=article.get("url"),
                title=article.get("title"),
                published_at=seen,
                confidence="title",
            )
        )
    unique = len({mention.url for mention in mentions if mention.url})
    return unique, mentions, errors


def _parse_story_datetime(story: dict) -> datetime | None:
    for key in ("publish_date", "published_date", "publishDate", "media_published_date", "date", "created_at"):
        value = story.get(key)
        if not value:
            continue
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = parsedate_to_datetime(str(value))
            except (TypeError, ValueError):
                continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


def query_mediacloud(candidate: Candidate, days: int) -> tuple[int, list[Mention], list[str]]:
    token = os.getenv("MEDIACLOUD_API_KEY")
    if not token:
        return 0, [], ["mediacloud:skipped:MEDIACLOUD_API_KEY not set"]
    # Media Cloud has had endpoint/version churn. Keep this isolated so the rest
    # of the pipeline remains useful if credentials or endpoint shape change.
    errors: list[str] = []
    mentions: list[Mention] = []
    headers = {"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT}
    for query in canonical_variants(candidate.heat_url):
        url = "https://api.mediacloud.org/api/search/story-list"
        params = {"q": f'"{query}"', "page_size": 100}
        try:
            response = requests.get(url, headers=headers, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            errors.append(f"mediacloud:{query}:{exc}")
            continue
        stories = data.get("stories") or data.get("results") or []
        for story in stories:
            published = _parse_story_datetime(story)
            if published is None or published < _cutoff(days):
                continue
            mentions.append(
                Mention(
                    provider="mediacloud",
                    query=query,
                    url=story.get("url") or story.get("story_url"),
                    title=story.get("title"),
                    published_at=published,
                    confidence="exact_url",
                )
            )
    return len({m.url for m in mentions if m.url}), mentions, errors


def _cutoff(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def query_bluesky(client: HttpClient, candidate: Candidate, days: int, limit: int = 25) -> tuple[int, list[Mention], list[str]]:
    mentions: list[Mention] = []
    errors: list[str] = []
    for query in canonical_variants(candidate.heat_url)[:2]:
        try:
            data = client.get(
                "https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts",
                params={"q": query, "limit": str(limit)},
            ).json()
        except Exception as exc:
            errors.append(f"bluesky:{query}:{exc}")
            continue
        for post in data.get("posts", []):
            record = post.get("record", {})
            uri = post.get("uri")
            published = None
            if record.get("createdAt"):
                try:
                    published = datetime.fromisoformat(record["createdAt"].replace("Z", "+00:00"))
                except ValueError:
                    published = None
            if published and published < _cutoff(days):
                continue
            mentions.append(
                Mention(
                    provider="bluesky",
                    query=query,
                    url=uri,
                    title=(record.get("text") or "")[:160],
                    published_at=published,
                    confidence="exact_url",
                )
            )
    return len({m.url for m in mentions if m.url}), mentions, errors


def _heat_queries(candidate: Candidate, expanded_variants: bool = False) -> list[tuple[str, str]]:
    queries: list[tuple[str, str]] = []
    if expanded_variants:
        queries.extend((url, "exact_url") for url in canonical_variants(candidate.heat_url))
    else:
        queries.append((candidate.heat_url, "exact_url"))
    name = filename_from_url(candidate.heat_url)
    if name and len(name) >= 12:
        queries.append((name, "filename"))
    seen: set[tuple[str, str]] = set()
    return [query for query in queries if not (query in seen or seen.add(query))]


def _unique_mentions(mentions: list[Mention]) -> list[Mention]:
    seen: set[tuple[str, str, str, str, str]] = set()
    unique: list[Mention] = []
    for mention in mentions:
        key = (
            mention.provider,
            mention.query,
            mention.url or "",
            mention.title or "",
            mention.confidence,
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(mention)
    return unique


def _add_mentions(result: HeatResult, mentions: list[Mention], social: bool = False) -> None:
    for mention in _unique_mentions(mentions):
        result.mentions.append(mention)
        if social:
            result.social_exact_mentions += 1
        elif mention.confidence == "title":
            result.coverage_mentions += 1
        elif mention.confidence == "filename":
            result.filename_mentions += 1
        elif mention.confidence == "redirect_or_canonical":
            result.canonical_mentions += 1
        else:
            result.exact_url_mentions += 1


def query_google_news(client: HttpClient, candidate: Candidate, days: int, limit: int = 20) -> tuple[int, list[Mention], list[str]]:
    mentions: list[Mention] = []
    errors: list[str] = []
    for query, confidence in _heat_queries(candidate):
        try:
            response = client.get(
                "https://news.google.com/rss/search",
                params={"q": f'"{query}"', "hl": "en-US", "gl": "US", "ceid": "US:en"},
            )
        except Exception as exc:
            errors.append(f"googlenews:{query}:{exc}")
            continue
        soup = BeautifulSoup(response.text, "xml")
        for item in soup.find_all("item")[:limit]:
            title_node = item.find("title")
            link_node = item.find("link")
            pub_node = item.find("pubDate")
            published = None
            if pub_node and pub_node.text:
                try:
                    published = parsedate_to_datetime(pub_node.text)
                except (TypeError, ValueError):
                    published = None
            if published and published < _cutoff(days):
                continue
            mentions.append(
                Mention(
                    provider="googlenews",
                    query=query,
                    url=link_node.text if link_node else None,
                    title=title_node.text if title_node else None,
                    published_at=published,
                    confidence=confidence,  # type: ignore[arg-type]
                )
            )
    return len({(m.url, m.query) for m in mentions if m.url}), mentions, errors


def query_hackernews(client: HttpClient, candidate: Candidate, days: int, limit: int = 20) -> tuple[int, list[Mention], list[str]]:
    mentions: list[Mention] = []
    errors: list[str] = []
    for query, confidence in _heat_queries(candidate):
        try:
            data = client.get(
                "https://hn.algolia.com/api/v1/search_by_date",
                params={
                    "query": query,
                    "hitsPerPage": str(limit),
                    "tags": "story,comment",
                    "numericFilters": f"created_at_i>{int(_cutoff(days).timestamp())}",
                },
            ).json()
        except Exception as exc:
            errors.append(f"hackernews:{query}:{exc}")
            continue
        for hit in data.get("hits", []):
            title = hit.get("title") or hit.get("comment_text") or hit.get("story_title")
            created = None
            if hit.get("created_at"):
                try:
                    created = datetime.fromisoformat(hit["created_at"].replace("Z", "+00:00"))
                except ValueError:
                    created = None
            mentions.append(
                Mention(
                    provider="hackernews",
                    query=query,
                    url=hit.get("url") or hit.get("story_url"),
                    title=title,
                    published_at=created,
                    confidence=confidence,  # type: ignore[arg-type]
                )
            )
    return len({(m.url, m.title, m.query) for m in mentions}), mentions, errors


def query_common_crawl(client: HttpClient, candidate: Candidate, index: str = "CC-MAIN-2026-18") -> tuple[int, list[str]]:
    errors: list[str] = []
    hits = 0
    for url in canonical_variants(candidate.heat_url)[:2]:
        endpoint = f"https://index.commoncrawl.org/{index}-index"
        try:
            response = client.get(endpoint, params={"url": url, "output": "json", "limit": "5"})
            lines = [line for line in response.text.splitlines() if line.strip()]
            hits += len(lines)
        except Exception as exc:
            errors.append(f"commoncrawl:{url}:{exc}")
    return hits, errors


def collect_heat(
    client: HttpClient,
    candidate: Candidate,
    days: int = 30,
    providers: set[str] | None = None,
    expanded_gdelt_variants: bool = False,
) -> HeatResult:
    providers = providers or {"gdelt"}
    result = HeatResult(candidate_url=candidate.heat_url, window_days=days)
    if "gdelt" in providers:
        # Primary signal: real news coverage matched on the report's title.
        _, cov_mentions, cov_errors = query_gdelt_coverage(client, candidate, days)
        result.providers_checked.append("gdelt")
        result.errors.extend(cov_errors)
        _add_mentions(result, cov_mentions)
        # Optional high-confidence exact-link/filename pickup (off by default).
        if expanded_gdelt_variants:
            _, gdelt_mentions, gdelt_errors = query_gdelt(
                client, candidate, days, expanded_variants=expanded_gdelt_variants
            )
            result.errors.extend(gdelt_errors)
            _add_mentions(result, gdelt_mentions)

    if "mediacloud" in providers:
        _, mc_mentions, mc_errors = query_mediacloud(candidate, days)
        result.providers_checked.append("mediacloud")
        result.errors.extend(mc_errors)
        _add_mentions(result, mc_mentions)

    if "bluesky" in providers:
        _, bluesky_mentions, bluesky_errors = query_bluesky(client, candidate, days)
        result.providers_checked.append("bluesky")
        result.errors.extend(bluesky_errors)
        _add_mentions(result, bluesky_mentions, social=True)

    if "googlenews" in providers:
        _, news_mentions, news_errors = query_google_news(client, candidate, days)
        result.providers_checked.append("googlenews")
        result.errors.extend(news_errors)
        _add_mentions(result, news_mentions)

    if "hackernews" in providers:
        _, hn_mentions, hn_errors = query_hackernews(client, candidate, days)
        result.providers_checked.append("hackernews")
        result.errors.extend(hn_errors)
        _add_mentions(result, hn_mentions)

    if "commoncrawl" in providers:
        result.providers_checked.append("commoncrawl")
        result.errors.append("commoncrawl:skipped:not available as a rolling-window heat signal")
    return result


def heat_score(result: HeatResult) -> float:
    return (
        6.0 * result.exact_url_mentions
        + 3.0 * result.canonical_mentions
        + 2.0 * min(result.filename_mentions, 5)
        + 2.0 * result.social_exact_mentions
        + 1.0 * min(result.coverage_mentions, 30)
    )


def heat_window_key(days: int) -> str:
    return "today" if days == 1 else f"{days}d"
