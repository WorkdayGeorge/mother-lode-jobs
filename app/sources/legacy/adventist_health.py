from __future__ import annotations

import html
import re
from datetime import datetime
from typing import Any

import requests

from ._compat import JobProvider
from ._compat import RawJob


class AdventistHealthProvider(JobProvider):
    """Adventist Health jobs from its public Oracle Recruiting Cloud feed."""

    BASE_URL = "https://ecvz.fa.us2.oraclecloud.com"
    SITE_NUMBER = "CX_1"
    SEARCH_URL = (
        f"{BASE_URL}/hcmRestApi/resources/latest/"
        "recruitingCEJobRequisitions"
    )
    DETAIL_URL = (
        f"{BASE_URL}/hcmRestApi/resources/latest/"
        "recruitingCEJobRequisitionDetails"
    )
    CAREERS_URL = (
        f"{BASE_URL}/hcmUI/CandidateExperience/en/sites/{SITE_NUMBER}"
    )

    # Keep Adventist tightly scoped to the candidate's foothill commute area.
    LOCALITIES = {
        "angels camp",
        "arnold",
        "avery",
        "copperopolis",
        "jamestown",
        "murphys",
        "san andreas",
        "sonora",
        "twain harte",
        "valley springs",
    }

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": (
                    "Mozilla/5.0 (compatible; CalaverasJobAgent/1.0)"
                ),
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
    def _parse_timestamp(value: Any) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else None

    @classmethod
    def _is_local(cls, location: Any) -> bool:
        value = str(location or "").lower()
        return any(locality in value for locality in cls.LOCALITIES)

    def _search_page(self, offset: int, limit: int = 100) -> dict:
        response = self.session.get(
            self.SEARCH_URL,
            params={
                "finder": (
                    "findReqs;"
                    f"siteNumber={self.SITE_NUMBER},"
                    f"limit={limit},offset={offset},"
                    "latitude=38.1960,longitude=-120.6805,"
                    "radius=65,radiusUnit=MI"
                ),
                "onlyData": "true",
                "expand": "requisitionList",
            },
            timeout=30,
        )
        response.raise_for_status()
        items = response.json().get("items") or []
        return items[0] if items else {}

    def _fetch_detail(self, job_id: str) -> dict:
        response = self.session.get(
            self.DETAIL_URL,
            params={
                "finder": (
                    f"ById;Id={job_id},siteNumber={self.SITE_NUMBER}"
                ),
                "onlyData": "true",
            },
            timeout=30,
        )
        response.raise_for_status()
        items = response.json().get("items") or []
        return items[0] if items else {}

    def _to_job(self, summary: dict, detail: dict) -> RawJob | None:
        job_id = str(detail.get("Id") or summary.get("Id") or "").strip()
        title = str(detail.get("Title") or summary.get("Title") or "").strip()
        location = str(
            detail.get("PrimaryLocation")
            or summary.get("PrimaryLocation")
            or ""
        ).strip()

        if not job_id or not title or not self._is_local(location):
            return None

        description = self._clean_html(
            detail.get("ExternalDescriptionStr")
            or summary.get("ShortDescriptionStr")
        )
        requirements = self._clean_html(
            detail.get("ExternalQualificationsStr")
        )
        posted_raw = detail.get("ExternalPostedStartDate")
        posted_at = self._parse_timestamp(posted_raw)
        apply_url = f"{self.CAREERS_URL}/job/{job_id}"

        return RawJob(
            provider_job_id=job_id,
            title=title,
            company="Adventist Health",
            location=location,
            employment_type=(
                detail.get("JobSchedule")
                or summary.get("JobSchedule")
                or detail.get("JobType")
                or None
            ),
            description=description,
            posted_at=posted_at,
            apply_url=apply_url,
            source="adventist_health",
            source_url=self.CAREERS_URL,
            requirements=[requirements] if requirements else [],
            metadata={
                "requisition_id": detail.get("RequisitionId"),
                "category": detail.get("Category"),
                "job_function": detail.get("JobFunction"),
                "job_shift": detail.get("JobShift"),
                "posting_end_date": detail.get("ExternalPostedEndDate"),
                "timestamp_note": (
                    "Verified exact timestamp from Oracle Recruiting Cloud's "
                    "ExternalPostedStartDate."
                    if posted_at
                    else "Oracle did not provide a trustworthy exact posting timestamp."
                ),
            },
        )

    def _load_jobs(self) -> list[RawJob]:
        if self._jobs is not None:
            return self._jobs

        summaries: dict[str, dict] = {}
        offset = 0
        limit = 100

        while True:
            payload = self._search_page(offset, limit)
            rows = payload.get("requisitionList") or []
            for row in rows:
                job_id = str(row.get("Id") or "").strip()
                if job_id and self._is_local(row.get("PrimaryLocation")):
                    summaries[job_id] = row

            offset += len(rows)
            total = int(payload.get("TotalJobsCount") or 0)
            if not rows or offset >= total:
                break

        jobs: list[RawJob] = []
        for job_id, summary in summaries.items():
            try:
                detail = self._fetch_detail(job_id)
            except (requests.RequestException, ValueError):
                # A bad detail page should not discard other Adventist jobs.
                continue
            job = self._to_job(summary, detail)
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
        jobs = self._load_jobs()
        tokens = {
            token
            for token in re.findall(r"[a-z0-9]+", (role or "").lower())
            if len(token) >= 3
        }
        if not tokens:
            return jobs[:results_per_page]

        matches = []
        for job in jobs:
            haystack = f"{job.title} {job.description or ''}".lower()
            if any(token in haystack for token in tokens):
                matches.append(job)
        return matches[:results_per_page]
