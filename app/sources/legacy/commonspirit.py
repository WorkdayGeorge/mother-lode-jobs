from __future__ import annotations

import html
import json
import re
from urllib.parse import urljoin

import requests

from ._compat import JobProvider
from ._compat import RawJob


class CommonSpiritProvider(JobProvider):
    LIST_URL = (
        "https://www.commonspirit.careers/"
        "location/san-andreas-jobs/"
        "35300/6252001-5332921-5391597/4"
    )

    BASE_URL = "https://www.commonspirit.careers"

    def __init__(self) -> None:
        self._cached_jobs: list[RawJob] | None = None
        self.session = requests.Session()
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 "
                "(compatible; CalaverasJobAgent/1.0)"
            )
        }

    @staticmethod
    def _clean_html(value: str | None) -> str:
        if not value:
            return ""

        value = html.unescape(value)
        value = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
        value = re.sub(r"</p>", "\n", value, flags=re.I)
        value = re.sub(r"</li>", "\n", value, flags=re.I)
        value = re.sub(r"<[^>]+>", " ", value)
        value = re.sub(r"[ \t]+", " ", value)
        value = re.sub(r"\n\s*", "\n", value)
        return value.strip()

    @staticmethod
    def _json_ld(page_html: str) -> dict | None:
        blocks = re.findall(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>'
            r'(.*?)</script>',
            page_html,
            flags=re.I | re.S,
        )

        for raw in blocks:
            try:
                data = json.loads(html.unescape(raw).strip())
            except Exception:
                continue

            if isinstance(data, dict) and data.get("@type") == "JobPosting":
                return data

            if isinstance(data, list):
                for item in data:
                    if (
                        isinstance(item, dict)
                        and item.get("@type") == "JobPosting"
                    ):
                        return item

        return None

    @staticmethod
    def _salary_text(base_salary) -> str | None:
        if not isinstance(base_salary, dict):
            return None

        currency = base_salary.get("currency", "USD")
        value = base_salary.get("value")

        if not isinstance(value, dict):
            return None

        minimum = value.get("minValue")
        maximum = value.get("maxValue")
        unit = value.get("unitText")

        if minimum is None and maximum is None:
            return None

        if minimum is not None and maximum is not None:
            text = f"${minimum:g} - ${maximum:g}"
        elif minimum is not None:
            text = f"${minimum:g}"
        else:
            text = f"${maximum:g}"

        if unit:
            text += f" per {str(unit).lower()}"

        if currency and currency != "USD":
            text += f" {currency}"

        return text

    @staticmethod
    def _location_text(job_location) -> str:
        if not job_location:
            return "San Andreas, CA"

        locations = (
            job_location
            if isinstance(job_location, list)
            else [job_location]
        )

        for place in locations:
            if not isinstance(place, dict):
                continue

            address = place.get("address")

            if not isinstance(address, dict):
                continue

            city = address.get("addressLocality")
            state = address.get("addressRegion")

            if city and state:
                return f"{str(city).title()}, {state}"

        return "San Andreas, CA"

    def _fetch_job(self, url: str) -> RawJob | None:
        response = self.session.get(
            url,
            headers=self.headers,
            timeout=30,
        )
        response.raise_for_status()

        data = self._json_ld(response.text)

        if not data:
            return None

        title = data.get("title") or "Untitled role"

        hiring_org = data.get("hiringOrganization") or {}
        company = (
            hiring_org.get("name")
            if isinstance(hiring_org, dict)
            else None
        ) or "Mark Twain Medical Center"

        identifier = data.get("identifier")

        if isinstance(identifier, dict):
            provider_job_id = (
                identifier.get("value")
                or identifier.get("name")
                or url
            )
        else:
            provider_job_id = str(identifier or url)

        description = self._clean_html(
            data.get("description")
        )

        employment_type = data.get("employmentType")

        if isinstance(employment_type, list):
            employment_type = ", ".join(
                str(x) for x in employment_type
            )

        salary = self._salary_text(
            data.get("baseSalary")
        )

        location = self._location_text(
            data.get("jobLocation")
        )

        date_posted = data.get("datePosted")
        valid_through = data.get("validThrough")

        return RawJob(
            provider_job_id=provider_job_id,
            title=title,
            company=company,
            location=location,
            employment_type=employment_type,
            description=description,
            posted_at=None,
            apply_url=url,
            source="commonspirit",
            source_url=url,
            requirements=[],
            metadata={
                "identifier": provider_job_id,
                "date_posted": date_posted,
                "valid_through": valid_through,
                "salary": salary,
                "timestamp_note": (
                    "CommonSpirit provides a posting date "
                    "without an exact posting time. "
                    "Exact original posting timestamp is "
                    "therefore not verified."
                ),
            },
        )

    def _load_jobs(self) -> list[RawJob]:
        response = self.session.get(
            self.LIST_URL,
            headers=self.headers,
            timeout=30,
        )
        response.raise_for_status()

        links = re.findall(
            r'href=["\']([^"\']*/job/san-andreas/[^"\']+)["\']',
            response.text,
            flags=re.I,
        )

        unique_links = []

        for link in links:
            full_url = urljoin(self.BASE_URL, link)

            if full_url not in unique_links:
                unique_links.append(full_url)

        jobs: list[RawJob] = []

        for url in unique_links:
            try:
                job = self._fetch_job(url)
            except requests.RequestException:
                continue

            if not job:
                continue

            # Keep this provider tightly scoped to Mark Twain /
            # San Andreas local openings.
            location = (job.location or "").lower()

            if (
                "san andreas" not in location
                and "calaveras" not in location
            ):
                continue

            jobs.append(job)

        return jobs

    def search(
        self,
        *,
        role: str,
        location: str,
        results_per_page: int = 25,
    ) -> list[RawJob]:

        if self._cached_jobs is None:
            self._cached_jobs = self._load_jobs()

        role_words = {
            word.lower()
            for word in re.findall(r"[A-Za-z0-9]+", role)
            if len(word) >= 3
        }

        if not role_words:
            return self._cached_jobs[:results_per_page]

        matches: list[RawJob] = []

        for job in self._cached_jobs:
            haystack = (
                f"{job.title} "
                f"{job.company} "
                f"{job.description or ''}"
            ).lower()

            if any(word in haystack for word in role_words):
                matches.append(job)

        return matches[:results_per_page]
