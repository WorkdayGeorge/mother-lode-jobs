from __future__ import annotations

import html
import json
import re
from typing import Any
from urllib.parse import urljoin

import requests

from ._compat import JobProvider
from ._compat import RawJob


class PGEProvider(JobProvider):
    """PG&E jobs from the employer's official Radancy careers site."""

    BASE_URL = "https://jobs.pge.com"
    SEARCH_URL = f"{BASE_URL}/search-jobs"

    # Calaveras County plus the surrounding Sierra foothill employment area.
    LOCALITIES = {
        "angels camp",
        "arnold",
        "auburn",
        "avery",
        "copperopolis",
        "ione",
        "jackson",
        "jamestown",
        "mokelumne hill",
        "murphys",
        "placerville",
        "san andreas",
        "sonora",
        "sutter creek",
        "twain harte",
        "valley springs",
    }

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (compatible; CalaverasJobAgent/1.0)",
                "Accept": "text/html,application/xhtml+xml",
            }
        )
        self._jobs: list[RawJob] | None = None

    @staticmethod
    def _clean_html(value: Any) -> str:
        if not value:
            return ""
        text = html.unescape(str(value))
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
        text = re.sub(r"</(?:p|li|div)>", "\n", text, flags=re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*", "\n", text)
        return text.strip()

    @staticmethod
    def _json_ld(page_html: str) -> dict | None:
        blocks = re.findall(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>'
            r"(.*?)</script>",
            page_html,
            flags=re.I | re.S,
        )
        for raw in blocks:
            try:
                value = json.loads(html.unescape(raw).strip())
            except (json.JSONDecodeError, TypeError):
                continue
            values = value if isinstance(value, list) else [value]
            for item in values:
                if isinstance(item, dict) and item.get("@type") == "JobPosting":
                    return item
        return None

    @classmethod
    def _is_local(cls, location: Any) -> bool:
        text = str(location or "").lower()
        return any(locality in text for locality in cls.LOCALITIES)

    @staticmethod
    def _location_text(job_location: Any) -> str:
        locations = job_location if isinstance(job_location, list) else [job_location]
        for place in locations:
            if not isinstance(place, dict):
                continue
            address = place.get("address")
            if not isinstance(address, dict):
                continue
            city = address.get("addressLocality")
            state = address.get("addressRegion")
            if city and state:
                return f"{city}, {state}"
        return ""

    def _fetch_job(self, url: str) -> RawJob | None:
        response = self.session.get(url, timeout=30)
        response.raise_for_status()
        data = self._json_ld(response.text)
        if not data:
            return None

        location = self._location_text(data.get("jobLocation"))
        if not self._is_local(location):
            return None

        identifier = data.get("identifier")
        if isinstance(identifier, dict):
            identifier = identifier.get("value") or identifier.get("name")
        provider_job_id = str(identifier or url).strip()
        title = str(data.get("title") or "").strip()
        if not provider_job_id or not title:
            return None

        employment_type = data.get("employmentType")
        if isinstance(employment_type, list):
            employment_type = ", ".join(str(item) for item in employment_type)

        date_posted = data.get("datePosted")
        valid_through = data.get("validThrough")
        canonical_url = str(data.get("url") or url)

        return RawJob(
            provider_job_id=provider_job_id,
            title=title,
            company="PG&E",
            location=location,
            employment_type=str(employment_type) if employment_type else None,
            description=self._clean_html(data.get("description")),
            posted_at=None,
            apply_url=canonical_url,
            source="pge",
            source_url=self.SEARCH_URL,
            requirements=[],
            metadata={
                "date_posted": date_posted,
                "valid_through": valid_through,
                "timestamp_note": (
                    "PG&E provides a posting date without an exact time or "
                    "timezone; the exact original posting timestamp is unverified."
                ),
            },
        )

    @staticmethod
    def _listing_links(page_html: str) -> list[tuple[str, str]]:
        rows = re.findall(
            r'<a class="search-results-list__job-link"\s+'
            r'href="([^"]+)"[^>]*>.*?</a>.*?'
            r'<li class="search-results-list__job-info job-location">'
            r"(.*?)</li>",
            page_html,
            flags=re.I | re.S,
        )
        return [(link, PGEProvider._clean_html(location)) for link, location in rows]

    def _load_jobs(self) -> list[RawJob]:
        if self._jobs is not None:
            return self._jobs

        first = self.session.get(self.SEARCH_URL, timeout=30)
        first.raise_for_status()
        page_html = first.text
        match = re.search(r'data-total-pages="(\d+)"', page_html)
        total_pages = max(1, int(match.group(1)) if match else 1)
        links: dict[str, str] = {}

        for page in range(1, total_pages + 1):
            if page > 1:
                try:
                    response = self.session.get(
                        self.SEARCH_URL, params={"p": page}, timeout=30
                    )
                    response.raise_for_status()
                    page_html = response.text
                except requests.RequestException:
                    # Keep results from healthy pages and let other providers run.
                    continue
            for link, location in self._listing_links(page_html):
                if self._is_local(location):
                    links[urljoin(self.BASE_URL, link)] = location

        jobs: list[RawJob] = []
        for url in links:
            try:
                job = self._fetch_job(url)
            except (requests.RequestException, ValueError):
                continue
            if job:
                jobs.append(job)

        self._jobs = jobs
        return jobs

    def search(
        self,
        *,
        role: str,
        location: str,
        results_per_page: int = 25,
    ) -> list[RawJob]:
        # The local PG&E pool is small. Return it for scoring rather than risk
        # dropping a transferable role through brittle keyword filtering.
        return self._load_jobs()[:results_per_page]
