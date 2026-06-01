from __future__ import annotations

import re
from datetime import date

from dateutil import parser


DATE_RE = re.compile(
    r"(?:(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4})|(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4})|(?:\d{4}-\d{2}-\d{2})",
    re.IGNORECASE,
)


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return parser.parse(value, fuzzy=True).date()
    except (ValueError, OverflowError):
        return None


def extract_date(text: str | None) -> date | None:
    if not text:
        return None
    match = DATE_RE.search(" ".join(text.split()))
    return parse_date(match.group(0)) if match else None
