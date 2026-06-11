from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from nyc_report_heat.http import HttpClient
from nyc_report_heat.models import Candidate, HarvestedMention
from nyc_report_heat.urltools import canonical_variants, filename_from_url, normalize_url


# ---------------------------------------------------------------------------
# Mention store (data/mentions.jsonl): append-only, deduped on uid, so heat
# windows are computed from accumulated history rather than live re-queries.
# ---------------------------------------------------------------------------


def read_mention_store(path: Path) -> list[HarvestedMention]:
    if not path.exists():
        return []
    mentions = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                mentions.append(HarvestedMention.model_validate_json(line))
    return mentions


def append_mentions(path: Path, new: list[HarvestedMention]) -> tuple[int, int]:
    """Append unseen mentions; returns (added, total in store)."""
    existing = read_mention_store(path)
    seen = {m.uid for m in existing}
    added = [m for m in new if m.uid not in seen and not (seen.add(m.uid))]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for mention in added:
            fh.write(mention.model_dump_json() + "\n")
    return len(added), len(existing) + len(added)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def target_matches_domains(url: str, domains: list[str]) -> bool:
    """True when the URL belongs to one of the tracked gov domains.

    A domain entry may carry a path prefix ("nyc.gov/assets") to narrow broad
    hosts. Bare hosts match the host and its subdomains.
    """
    try:
        parsed = urlparse(url if "://" in url else f"https://{url}")
    except ValueError:
        return False
    host = parsed.netloc.lower().removeprefix("www.").removeprefix("www1.")
    path = parsed.path or "/"
    for entry in domains:
        dom, _, prefix = entry.partition("/")
        dom = dom.lower().removeprefix("www.")
        if host != dom and not host.endswith("." + dom):
            continue
        if not prefix or path.lower().startswith("/" + prefix.lower()):
            return True
    return False


# ---------------------------------------------------------------------------
# Bluesky: free, unauthenticated search. One paginated query per domain term;
# gov links are read from the post's link facets and external-card embed.
# ---------------------------------------------------------------------------

# public.api.bsky.app rejects unauthenticated search (403); api.bsky.app serves it.
BLUESKY_SEARCH = "https://api.bsky.app/xrpc/app.bsky.feed.searchPosts"


def _bluesky_post_links(post: dict) -> set[str]:
    record = post.get("record", {})
    links: set[str] = set()
    for facet in record.get("facets") or []:
        for feature in facet.get("features") or []:
            if str(feature.get("$type", "")).endswith("#link") and feature.get("uri"):
                links.add(feature["uri"])
    embed = record.get("embed") or {}
    external = embed.get("external") or {}
    if external.get("uri"):
        links.add(external["uri"])
    return links


def _bluesky_engagement(post: dict) -> int:
    return sum(int(post.get(key) or 0) for key in ("likeCount", "repostCount", "quoteCount", "replyCount"))


def _bluesky_post_url(post: dict) -> str | None:
    # at://did:plc:xyz/app.bsky.feed.post/rkey -> public web URL
    uri = post.get("uri") or ""
    match = re.match(r"at://([^/]+)/app\.bsky\.feed\.post/(.+)", uri)
    handle = (post.get("author") or {}).get("handle")
    if match and handle:
        return f"https://bsky.app/profile/{handle}/post/{match.group(2)}"
    return uri or None


def harvest_bluesky(
    client: HttpClient,
    domains: list[str],
    lookback_days: int = 30,
    max_pages: int = 8,
) -> tuple[list[HarvestedMention], list[str]]:
    mentions: list[HarvestedMention] = []
    errors: list[str] = []
    since = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    seen_hosts: set[str] = set()
    for entry in domains:
        query = entry.partition("/")[0]  # search the bare host; paths rarely tokenize
        if query in seen_hosts:
            continue
        seen_hosts.add(query)
        cursor: str | None = None
        for _ in range(max_pages):
            params = {"q": query, "limit": "100", "sort": "latest", "since": since}
            if cursor:
                params["cursor"] = cursor
            try:
                data = client.get(BLUESKY_SEARCH, params=params).json()
            except Exception as exc:
                errors.append(f"bluesky:{query}:{exc}")
                break
            posts = data.get("posts", [])
            for post in posts:
                published = _parse_iso((post.get("record") or {}).get("createdAt"))
                text = ((post.get("record") or {}).get("text") or "")[:200]
                for link in _bluesky_post_links(post):
                    if not target_matches_domains(link, domains):
                        continue
                    target = normalize_url(link)
                    mentions.append(
                        HarvestedMention(
                            uid=f"bluesky:{post.get('uri')}:{target}",
                            provider="bluesky",
                            query=query,
                            target_url=target,
                            source_url=_bluesky_post_url(post),
                            title=text,
                            author=(post.get("author") or {}).get("handle"),
                            published_at=published,
                            engagement=_bluesky_engagement(post),
                        )
                    )
            cursor = data.get("cursor")
            if not cursor or not posts:
                break
    return mentions, errors


# ---------------------------------------------------------------------------
# Hacker News via Algolia: free, no auth. One URL-restricted query per domain.
# ---------------------------------------------------------------------------

HN_SEARCH = "https://hn.algolia.com/api/v1/search_by_date"


def harvest_hackernews(
    client: HttpClient,
    domains: list[str],
    lookback_days: int = 30,
) -> tuple[list[HarvestedMention], list[str]]:
    mentions: list[HarvestedMention] = []
    errors: list[str] = []
    cutoff = int((datetime.now(timezone.utc) - timedelta(days=lookback_days)).timestamp())
    seen_hosts: set[str] = set()
    for entry in domains:
        query = entry.partition("/")[0]
        if query in seen_hosts:
            continue
        seen_hosts.add(query)
        params = {
            "query": query,
            "tags": "story",
            "restrictSearchableAttributes": "url",
            "hitsPerPage": "100",
            "numericFilters": f"created_at_i>{cutoff}",
        }
        try:
            data = client.get(HN_SEARCH, params=params).json()
        except Exception as exc:
            errors.append(f"hackernews:{query}:{exc}")
            continue
        for hit in data.get("hits", []):
            url = hit.get("url")
            if not url or not target_matches_domains(url, domains):
                continue
            mentions.append(
                HarvestedMention(
                    uid=f"hackernews:{hit.get('objectID')}",
                    provider="hackernews",
                    query=query,
                    target_url=normalize_url(url),
                    source_url=f"https://news.ycombinator.com/item?id={hit.get('objectID')}",
                    title=hit.get("title"),
                    author=hit.get("author"),
                    published_at=_parse_iso(hit.get("created_at")),
                    engagement=int(hit.get("points") or 0) + int(hit.get("num_comments") or 0),
                )
            )
    return mentions, errors


# ---------------------------------------------------------------------------
# Reddit: where NYC reports actually get discussed (r/nyc, r/AskNYC, ...).
# Unauthenticated JSON is now 403'd, but the OAuth API is free and CI-safe.
# Register a "script" app at reddit.com/prefs/apps and set REDDIT_CLIENT_ID /
# REDDIT_CLIENT_SECRET; without them this provider skips cleanly.
# ---------------------------------------------------------------------------

REDDIT_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
REDDIT_API = "https://oauth.reddit.com"
REDDIT_UA = "nyc-report-heat/0.2 (by u/nyc-report-heat; github.com/gothamizer/nyc-report-heat)"


def _reddit_token(client_id: str, client_secret: str) -> str:
    resp = requests.post(
        REDDIT_TOKEN_URL,
        auth=(client_id, client_secret),
        data={"grant_type": "client_credentials"},
        headers={"User-Agent": REDDIT_UA},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def harvest_reddit(
    client: HttpClient,
    domains: list[str],
    lookback_days: int = 30,
) -> tuple[list[HarvestedMention], list[str]]:
    client_id = os.getenv("REDDIT_CLIENT_ID")
    client_secret = os.getenv("REDDIT_CLIENT_SECRET")
    if not client_id or not client_secret:
        return [], ["reddit:skipped:REDDIT_CLIENT_ID/REDDIT_CLIENT_SECRET not set"]
    try:
        token = _reddit_token(client_id, client_secret)
    except Exception as exc:
        return [], [f"reddit:token:{exc}"]
    headers = {"Authorization": f"bearer {token}", "User-Agent": REDDIT_UA}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).timestamp()
    mentions: list[HarvestedMention] = []
    errors: list[str] = []
    seen_hosts: set[str] = set()
    for entry in domains:
        host = entry.partition("/")[0]  # domain listing is per host; path-filter below
        if host in seen_hosts:
            continue
        seen_hosts.add(host)
        try:
            data = client.get(
                f"{REDDIT_API}/domain/{host}/new",
                params={"limit": "100"},
                headers=headers,
            ).json()
        except Exception as exc:
            errors.append(f"reddit:{host}:{exc}")
            continue
        for child in data.get("data", {}).get("children", []):
            post = child.get("data", {})
            url = post.get("url")
            if not url or not target_matches_domains(url, domains):
                continue
            created = post.get("created_utc")
            published = (
                datetime.fromtimestamp(created, timezone.utc) if created else None
            )
            if published is not None and published.timestamp() < cutoff:
                continue
            permalink = post.get("permalink")
            mentions.append(
                HarvestedMention(
                    uid=f"reddit:{post.get('id')}:{normalize_url(url)}",
                    provider="reddit",
                    query=host,
                    target_url=normalize_url(url),
                    source_url=f"https://www.reddit.com{permalink}" if permalink else None,
                    title=post.get("title"),
                    author=f"r/{post.get('subreddit')}" if post.get("subreddit") else None,
                    published_at=published,
                    engagement=int(post.get("score") or 0) + int(post.get("num_comments") or 0),
                )
            )
    return mentions, errors


# ---------------------------------------------------------------------------
# NYC press: poll outlet RSS feeds, fetch new articles once, and extract
# outbound links to the tracked gov domains. This measures the citation
# directly instead of asking a search engine whether it happened.
# ---------------------------------------------------------------------------

HREF_RE = re.compile(r"https?://[^\s\"'<>\\)\\]]+", re.IGNORECASE)


def _feed_items(xml_text: str) -> list[dict]:
    soup = BeautifulSoup(xml_text, "xml")
    items = []
    for node in soup.find_all(["item", "entry"]):
        link_node = node.find("link")
        link = None
        if link_node is not None:
            link = (link_node.text or "").strip() or link_node.get("href")
        title_node = node.find("title")
        date_node = node.find(["pubDate", "published", "updated", "dc:date"])
        published = None
        if date_node and date_node.text:
            try:
                published = parsedate_to_datetime(date_node.text.strip())
            except (TypeError, ValueError):
                published = _parse_iso(date_node.text.strip())
        if published is not None and published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        content_node = node.find(["content:encoded", "content", "description"])
        items.append(
            {
                "url": link,
                "title": title_node.text.strip() if title_node and title_node.text else None,
                "published_at": published,
                "inline_html": content_node.text if content_node and content_node.text else "",
            }
        )
    return items


def read_article_ledger(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}


def append_article_ledger(path: Path, urls: list[str]) -> None:
    if not urls:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for url in urls:
            fh.write(url + "\n")


def _extract_gov_links(html: str, domains: list[str]) -> set[str]:
    links: set[str] = set()
    soup = BeautifulSoup(html, "html.parser")
    for anchor in soup.find_all("a", href=True):
        if target_matches_domains(anchor["href"], domains):
            links.add(anchor["href"])
    # Catch URLs that appear outside anchors (JSON blobs, plain text).
    for raw in HREF_RE.findall(html):
        if target_matches_domains(raw, domains):
            links.add(raw.rstrip(".,;"))
    return links


def harvest_news_feeds(
    client: HttpClient,
    feeds: list[str],
    domains: list[str],
    ledger_path: Path,
    lookback_days: int = 14,
) -> tuple[list[HarvestedMention], list[str]]:
    mentions: list[HarvestedMention] = []
    errors: list[str] = []
    seen_articles = read_article_ledger(ledger_path)
    checked: list[str] = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    for feed_url in feeds:
        try:
            feed_text = client.get(feed_url).text
        except Exception as exc:
            errors.append(f"newsrss:{feed_url}:{exc}")
            continue
        outlet = urlparse(feed_url).netloc.removeprefix("www.").removeprefix("rss.")
        for item in _feed_items(feed_text):
            article_url = item["url"]
            if not article_url:
                continue
            article_key = normalize_url(article_url)
            if article_key in seen_articles:
                continue
            if item["published_at"] and item["published_at"] < cutoff:
                continue
            html = item["inline_html"]
            try:
                html += client.get(article_url).text
            except Exception as exc:
                errors.append(f"newsrss:{article_url}:{exc}")
                # Feed-inline content may still carry the links; fall through.
            seen_articles.add(article_key)
            checked.append(article_key)
            for link in _extract_gov_links(html, domains):
                target = normalize_url(link)
                mentions.append(
                    HarvestedMention(
                        uid=f"newsrss:{article_key}:{target}",
                        provider="newsrss",
                        query=feed_url,
                        target_url=target,
                        source_url=article_url,
                        title=item["title"],
                        author=outlet,
                        published_at=item["published_at"],
                    )
                )
    append_article_ledger(ledger_path, checked)
    return mentions, errors


# ---------------------------------------------------------------------------
# Matching: join harvested gov links against the tracked inventory locally.
# ---------------------------------------------------------------------------


def build_candidate_index(candidates: list[Candidate]) -> tuple[dict[str, str], dict[str, str]]:
    """Returns (url variant -> candidate key, distinctive filename -> candidate key)."""
    by_url: dict[str, str] = {}
    by_filename: dict[str, str] = {}
    for candidate in candidates:
        key = normalize_url(candidate.heat_url)
        urls = {candidate.heat_url, candidate.url}
        if candidate.document_url:
            urls.add(candidate.document_url)
        for url in urls:
            for variant in canonical_variants(url):
                by_url.setdefault(variant, key)
            by_url.setdefault(normalize_url(url), key)
        name = filename_from_url(candidate.heat_url)
        if name and len(name) >= 12:
            by_filename.setdefault(name.lower(), key)
    return by_url, by_filename


def match_mention(
    mention: HarvestedMention,
    by_url: dict[str, str],
    by_filename: dict[str, str],
) -> tuple[str | None, str]:
    """Returns (candidate key, confidence) — confidence is exact_url | filename | none."""
    target = normalize_url(mention.target_url)
    if target in by_url:
        return by_url[target], "exact_url"
    stripped = target.split("?", 1)[0]
    if stripped in by_url:
        return by_url[stripped], "exact_url"
    name = filename_from_url(target)
    if name and name.lower() in by_filename:
        return by_filename[name.lower()], "filename"
    return None, "none"
