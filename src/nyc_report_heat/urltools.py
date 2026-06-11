from __future__ import annotations

from pathlib import PurePosixPath
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse


TRACKING_PREFIXES = ("utm_",)
TRACKING_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid"}


def absolutize(url: str, base: str) -> str:
    return urljoin(base, url.strip())


def normalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    scheme = "https" if parsed.scheme in {"http", "https", ""} else parsed.scheme
    netloc = parsed.netloc.lower()
    if netloc.startswith("www1."):
        netloc = "www." + netloc[5:]
    query_items = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        low = key.lower()
        if low in TRACKING_KEYS or any(low.startswith(prefix) for prefix in TRACKING_PREFIXES):
            continue
        query_items.append((key, value))
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    return urlunparse((scheme, netloc, path, "", urlencode(query_items), ""))


def filename_from_url(url: str) -> str | None:
    path = urlparse(url).path
    name = PurePosixPath(path).name
    return name or None


def detect_format(url: str) -> str:
    path = urlparse(url).path.lower()
    for ext in (".pdf", ".docx", ".xlsx"):
        if path.endswith(ext):
            return ext.removeprefix(".")
    if path.endswith((".page", ".html", ".htm")) or "." not in PurePosixPath(path).name:
        return "html"
    return "unknown"


def canonical_variants(url: str) -> list[str]:
    norm = normalize_url(url)
    parsed = urlparse(norm)
    hosts = {parsed.netloc}
    # the same document gets shared with and without www
    if parsed.netloc.startswith("www."):
        hosts.add(parsed.netloc[4:])
    else:
        hosts.add("www." + parsed.netloc)
    if parsed.netloc in {"home4.nyc.gov", "www.nyc.gov", "nyc.gov"}:
        hosts.update({"home4.nyc.gov", "www.nyc.gov", "nyc.gov"})
    variants = set()
    for host in hosts:
        variants.add(urlunparse((parsed.scheme, host, parsed.path, "", parsed.query, "")))
        if parsed.scheme == "https":
            variants.add(urlunparse(("http", host, parsed.path, "", parsed.query, "")))
    return sorted(variants)
