from __future__ import annotations

from datetime import datetime, timezone
from html.parser import HTMLParser
import html
import json
import re
from typing import Any
from urllib.parse import urljoin

import requests

from ._compat import JobProvider
from ._compat import RawJob


class _HarriParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []
        self._capture_json = False
        self._buffer: list[str] = []
        self.postings: list[dict[str, Any]] = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "a":
            href = attributes.get("href", "")
            if re.search(r"/job/\d+[-/]", href):
                self.links.append(href)
        if tag == "script" and attributes.get("type") == "application/ld+json":
            self._capture_json = True
            self._buffer = []

    def handle_endtag(self, tag):
        if tag != "script" or not self._capture_json:
            return
        self._capture_json = False
        try:
            payload = json.loads("".join(self._buffer))
        except (TypeError, ValueError):
            return
        items = payload if isinstance(payload, list) else [payload]
        self.postings.extend(
            item
            for item in items
            if isinstance(item, dict) and item.get("@type") == "JobPosting"
        )

    def handle_data(self, data):
        if self._capture_json:
            self._buffer.append(data)


def _clean(value: Any) -> str:
    if not value:
        return ""
    text = html.unescape(str(value))
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(?:p|li|div|h[1-6])>", "\n", text, flags=re.I)
    text = re.sub(r"<li[^>]*>", "- ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*", "\n", text).strip()


def _text(value: Any) -> str | None:
    if isinstance(value, list):
        result = ", ".join(str(item).strip() for item in value if str(item).strip())
        return result or None
    result = str(value or "").strip()
    return result or None


def _date(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result


class GreenhornCreekProvider(JobProvider):
    """Greenhorn Creek Resort openings from its Harri employer page."""

    COMPANY_URL = "https://harri.com/Yad-BmDiBaycxfQT"
    SITEMAP_URL = "https://harri.com/sitemap.xml"
    SOURCE_KEY = "greenhorn_creek"

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {"User-Agent": "CalaverasJobAgent/1.0", "Accept": "text/html"}
        )
        self._cached_jobs: list[RawJob] | None = None

    def _detail(self, url: str) -> tuple[dict[str, Any], str]:
        response = self.session.get(url, timeout=30)
        response.raise_for_status()
        parser = _HarriParser()
        parser.feed(response.text)
        return (parser.postings[0] if parser.postings else {}, response.text)

    def _sitemap_links(self) -> list[str]:
        """Use Harri's public sitemap when the Angular page has no static links."""
        response = self.session.get(self.SITEMAP_URL, timeout=30)
        response.raise_for_status()
        locations = self._locations(response.text)
        jobs = self._greenhorn_urls(locations)
        if jobs:
            return jobs

        # Some sitemap endpoints are indexes. Inspect their child maps, but
        # keep the request count bounded so a platform change cannot fan out.
        for sitemap_url in [url for url in locations if url.endswith(".xml")][:25]:
            try:
                child = self.session.get(sitemap_url, timeout=30)
                child.raise_for_status()
            except requests.RequestException:
                continue
            jobs.extend(self._greenhorn_urls(self._locations(child.text)))
        return list(dict.fromkeys(jobs))

    @staticmethod
    def _locations(body: str) -> list[str]:
        return [
            html.unescape(value).strip()
            for value in re.findall(r"<loc>(.*?)</loc>", body, re.I | re.S)
        ]

    @staticmethod
    def _greenhorn_urls(locations: list[str]) -> list[str]:
        pattern = re.compile(
            r"^https://(?:www\.)?harri\.com/Yad-BmDiBaycxfQT/job/\d+[-/]",
            re.I,
        )
        return [url for url in locations if pattern.search(url)]

    def _load_jobs(self) -> list[RawJob]:
        response = self.session.get(self.COMPANY_URL, timeout=30)
        response.raise_for_status()
        parser = _HarriParser()
        parser.feed(response.text)
        urls = list(dict.fromkeys(urljoin(self.COMPANY_URL, href) for href in parser.links))
        if not urls:
            urls = list(dict.fromkeys(self._sitemap_links()))
        jobs: list[RawJob] = []

        for url in urls:
            match = re.search(r"/job/(\d+)[-/]", url)
            if not match:
                continue
            try:
                posting, detail_html = self._detail(url)
            except (requests.RequestException, ValueError):
                continue
            if re.search(r"expired job post", detail_html, re.I):
                continue

            title = _text(posting.get("title"))
            if not title:
                slug = url.rsplit("/", 1)[-1]
                title = re.sub(r"^\d+-", "", slug).replace("-", " ").title()

            address = (posting.get("jobLocation") or {}).get("address") or {}
            location = ", ".join(
                str(value).strip()
                for value in (
                    address.get("addressLocality"),
                    address.get("addressRegion"),
                )
                if value
            ) or "Angels Camp, CA"

            jobs.append(
                RawJob(
                    provider_job_id=match.group(1),
                    title=title,
                    company="Greenhorn Creek Resort",
                    location=location,
                    employment_type=_text(posting.get("employmentType")),
                    description=_clean(posting.get("description")),
                    posted_at=_date(posting.get("datePosted")),
                    apply_url=url,
                    source=self.SOURCE_KEY,
                    source_url=url,
                    requirements=[],
                    metadata={
                        "hiring_platform": "Harri",
                        "timestamp_note": (
                            "Harri posting time is retained when supplied; "
                            "otherwise it remains unverified."
                        ),
                    },
                )
            )
        return jobs

    def search(self, *, role: str, location: str, results_per_page: int = 25):
        del location
        if self._cached_jobs is None:
            self._cached_jobs = self._load_jobs()
        words = {
            word.casefold()
            for word in re.findall(r"[A-Za-z0-9]+", role)
            if len(word) >= 3
        }
        if not words:
            return self._cached_jobs[:results_per_page]
        matches = [
            job
            for job in self._cached_jobs
            if any(
                word in f"{job.title} {job.description or ''}".casefold()
                for word in words
            )
        ]
        return matches[:results_per_page]
