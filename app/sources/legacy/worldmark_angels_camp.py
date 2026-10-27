from __future__ import annotations

import html
import json
import re
from datetime import datetime
from typing import Any

import requests

from ._compat import JobProvider
from ._compat import RawJob


class WorldMarkAngelsCampProvider(JobProvider):
    """WorldMark Angels Camp jobs from Travel + Leisure Co. careers."""

    BASE_URL = "https://careers.travelandleisureco.com"
    SEARCH_URL = f"{BASE_URL}/jobs/search"

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

    @staticmethod
    def _parse_timestamp(value: Any) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else None

    @staticmethod
    def _is_angels_camp(location: Any) -> bool:
        value = str(location or "").lower()
        return "angels camp" in value and (
            "california" in value or re.search(r"\bca\b", value) is not None
        )

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

    @classmethod
    def _listing_links(cls, page_html: str) -> list[str]:
        links: dict[str, None] = {}
        rows = re.findall(
            r'<tr[^>]+data-job-url="([^"]+)"[^>]*>(.*?)</tr>',
            page_html,
            flags=re.I | re.S,
        )
        for url, row in rows:
            location_match = re.search(
                r'aria-label="Location:\s*([^"]+)"', row, flags=re.I
            )
            if location_match and cls._is_angels_camp(location_match.group(1)):
                links[html.unescape(url)] = None
        return list(links)

    def _fetch_job(self, url: str) -> RawJob | None:
        response = self.session.get(url, timeout=30)
        response.raise_for_status()
        data = self._json_ld(response.text)
        if not data:
            return None

        location = self._location_text(data.get("jobLocation"))
        if not self._is_angels_camp(location):
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
        posted_raw = data.get("datePosted")
        posted_at = self._parse_timestamp(posted_raw)

        return RawJob(
            provider_job_id=provider_job_id,
            title=title,
            company="Travel + Leisure Co. / WorldMark Angels Camp",
            location=location,
            employment_type=str(employment_type) if employment_type else None,
            description=self._clean_html(data.get("description")),
            posted_at=posted_at,
            apply_url=url,
            source="worldmark_angels_camp",
            source_url=self.SEARCH_URL,
            requirements=[],
            metadata={
                "date_posted": posted_raw,
                "valid_through": data.get("validThrough"),
                "timestamp_note": (
                    "Verified exact UTC timestamp from the official Travel + "
                    "Leisure Co. JobPosting metadata."
                    if posted_at
                    else "No trustworthy exact posting timestamp was provided."
                ),
            },
        )

    def _load_jobs(self) -> list[RawJob]:
        if self._jobs is not None:
            return self._jobs

        response = self.session.get(
            self.SEARCH_URL, params={"query": "Angels Camp"}, timeout=30
        )
        response.raise_for_status()

        jobs: list[RawJob] = []
        seen_ids: set[str] = set()
        for url in self._listing_links(response.text):
            try:
                job = self._fetch_job(url)
            except (requests.RequestException, ValueError):
                # Keep healthy listings if a single detail page is unavailable.
                continue
            if job and job.provider_job_id not in seen_ids:
                seen_ids.add(job.provider_job_id or "")
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
        # The property has a small local pool, so send every opening to the
        # existing evaluator instead of applying brittle keyword filtering.
        return self._load_jobs()[:results_per_page]
