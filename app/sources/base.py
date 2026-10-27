"""Shared types for every job-source adapter.

An adapter is a function-like object that takes a source's ``config`` and
returns every currently open job at that source.  The worker calls each
source once per run; filtering by keyword happens on our side, not theirs.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from html.parser import HTMLParser
from typing import Any, Callable, ClassVar
from urllib.parse import urlparse

import requests

from app.config import get_settings


class SourceError(RuntimeError):
    """Raised when a source can't be read. The message is shown to admins."""


class ConfigError(SourceError):
    """The source row is missing something the adapter needs."""


@dataclass
class RawJob:
    external_id: str
    title: str
    apply_url: str
    employer: str | None = None
    location: str | None = None
    description: str | None = None
    employment_type: str | None = None
    salary_text: str | None = None
    posted_on: date | None = None
    closes_on: date | None = None
    attribution: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class FetchResult:
    jobs: list[RawJob]
    # For page_watch sources: a hash of the watched page.
    page_fingerprint: str | None = None
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ConfigField:
    name: str
    label: str
    required: bool = True
    help: str = ""
    kind: str = "text"  # text | list | int


class Http:
    """A requests session with a polite user agent, timeouts and retries."""

    RETRY_STATUSES = {429, 500, 502, 503, 504}

    def __init__(self, session: requests.Session | None = None, *, retries: int = 2, pause: float = 1.0):
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", get_settings().http_user_agent)
        self.retries = retries
        self.pause = pause

    def get(self, url: str, **kwargs) -> requests.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> requests.Response:
        return self.request("POST", url, **kwargs)

    def request(self, method: str, url: str, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", 30)
        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                response = self.session.request(method, url, **kwargs)
            except requests.RequestException as exc:
                last_exc = exc
            else:
                if response.status_code in self.RETRY_STATUSES and attempt < self.retries:
                    time.sleep(self.pause * (2**attempt))
                    continue
                if response.status_code >= 400:
                    raise SourceError(f"{url} returned HTTP {response.status_code}")
                return response
            if attempt < self.retries:
                time.sleep(self.pause * (2**attempt))
        reason = type(last_exc).__name__ if last_exc else "no response"
        raise SourceError(f"Couldn't reach {urlparse(url).netloc} ({reason})")

    def json(self, url: str, **kwargs) -> Any:
        response = self.get(url, **kwargs)
        try:
            return response.json()
        except ValueError as exc:
            raise SourceError(f"{url} did not return JSON") from exc


class Adapter:
    """Base class. Subclasses set the class attributes and implement fetch()."""

    key: ClassVar[str]
    label: ClassVar[str]
    description: ClassVar[str] = ""
    config_fields: ClassVar[tuple[ConfigField, ...]] = ()
    # True when one source row covers many employers (aggregators, EdJoin).
    multi_employer: ClassVar[bool] = False

    def __init__(self, config: dict[str, Any], *, source_name: str, county: str | None, http: Http):
        self.config = dict(config or {})
        self.source_name = source_name
        self.county = county
        self.http = http
        for spec in self.config_fields:
            if spec.required and not self.config.get(spec.name):
                raise ConfigError(f"Missing setting '{spec.label}' ({spec.name})")

    def fetch(self) -> FetchResult:  # pragma: no cover - interface
        raise NotImplementedError


# --------------------------------------------------------------------------
# small parsing helpers used by several adapters
# --------------------------------------------------------------------------


def parse_date(value: Any, *formats: str) -> date | None:
    if not value:
        return None
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    formats = formats or ("%Y-%m-%d", "%m/%d/%Y", "%d-%b-%Y", "%b %d, %Y", "%B %d, %Y", "%Y-%m-%d %H:%M:%S")
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    # ISO-ish with time/zone suffixes we don't care about, e.g. "2026-9-11"
    match = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if match:
        try:
            return date(int(match[1]), int(match[2]), int(match[3]))
        except ValueError:
            return None
    return None


class JsonLdParser(HTMLParser):
    """Collect every <script type="application/ld+json"> block on a page."""

    def __init__(self) -> None:
        super().__init__()
        self._capture = False
        self._buffer: list[str] = []
        self.blocks: list[Any] = []

    def handle_starttag(self, tag, attrs):
        if tag == "script" and (dict(attrs).get("type") or "").lower() == "application/ld+json":
            self._capture = True
            self._buffer = []

    def handle_endtag(self, tag):
        if tag == "script" and self._capture:
            self._capture = False
            raw = "".join(self._buffer).strip()
            try:
                self.blocks.append(json.loads(raw))
            except ValueError:
                pass

    def handle_data(self, data):
        if self._capture:
            self._buffer.append(data)


def iter_job_postings(html_text: str) -> list[dict[str, Any]]:
    """Every schema.org JobPosting object embedded in a page."""
    parser = JsonLdParser()
    parser.feed(html_text)
    found: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            kind = node.get("@type")
            kinds = kind if isinstance(kind, list) else [kind]
            if "JobPosting" in kinds:
                found.append(node)
            for key in ("@graph", "itemListElement", "item"):
                if key in node:
                    walk(node[key])

    for block in parser.blocks:
        walk(block)
    return found


def text_or_join(value: Any) -> str | None:
    if isinstance(value, (list, tuple, set)):
        joined = ", ".join(str(v).strip() for v in value if str(v).strip())
        return joined or None
    if value is None:
        return None
    text = str(value).strip()
    return text or None


AdapterFactory = Callable[..., Adapter]
