from __future__ import annotations

import html
import re
import unicodedata


def slugify(value: str, max_length: int = 80) -> str:
    value = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode()
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return value[:max_length].strip("-") or "job"


def clean_html(value: object) -> str:
    """Turn an HTML fragment into readable plain text with paragraph breaks."""
    if not value:
        return ""
    text = html.unescape(str(value))
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(p|div|h[1-6]|tr|ul|ol)>", "\n\n", text, flags=re.I)
    text = re.sub(r"<li[^>]*>", "\n• ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def squash(value: object) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def truncate(value: str | None, length: int = 220) -> str:
    value = squash(value)
    if len(value) <= length:
        return value
    cut = value[:length].rsplit(" ", 1)[0]
    return cut.rstrip(",.;:") + "…"


_MONEY = re.compile(r"\$?\s*(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?")
_UNITS = (
    ("hour", ("hour", "hr", "hourly")),
    ("year", ("year", "yr", "annual", "annually")),
    ("month", ("month", "mo", "monthly")),
    ("week", ("week", "weekly")),
    ("day", ("day", "daily")),
)


def parse_salary(text: str | None) -> tuple[float | None, float | None, str | None]:
    """Best-effort parse of '$22.50 - $28.00 per Hour' style strings."""
    if not text:
        return None, None, None
    amounts = []
    for whole, cents in _MONEY.findall(text):
        try:
            amounts.append(float(whole.replace(",", "") + (f".{cents}" if cents else "")))
        except ValueError:
            continue
    amounts = [a for a in amounts if a >= 7]  # skip stray small numbers
    if not amounts:
        return None, None, None
    lowered = text.lower()
    unit = None
    for name, words in _UNITS:
        if any(re.search(rf"\b{w}\b", lowered) for w in words):
            unit = name
            break
    low, high = min(amounts[:2]), max(amounts[:2])
    if unit is None:
        unit = "hour" if high < 200 else ("month" if high < 20000 else "year")
    return low, high, unit


def hourly_equivalent(amount: float | None, unit: str | None) -> float | None:
    if amount is None or unit is None:
        return None
    factor = {"hour": 1, "day": 1 / 8, "week": 1 / 40, "month": 12 / 2080, "year": 1 / 2080}.get(unit)
    return round(amount * factor, 2) if factor else None
