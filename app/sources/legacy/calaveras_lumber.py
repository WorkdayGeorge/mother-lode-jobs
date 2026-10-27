from __future__ import annotations

from datetime import datetime
import html
import json
import re
from typing import Any

import requests

from ._compat import JobProvider
from ._compat import RawJob


class CalaverasLumberProvider(JobProvider):
    """Official Calaveras & Sonora Lumber openings hosted by Paycom."""

    SOURCE_KEY = "calaveras_lumber"
    COMPANY_NAME = "Calaveras & Sonora Lumber"
    PORTAL_KEY = "11C30BF2C8590F32D1D813EA44A4CC1A"
    PORTAL_URL = (
        "https://www.paycomonline.net/v4/ats/web.php/portal/"
        f"{PORTAL_KEY}/career-page"
    )
    APPLY_BASE = (
        "https://www.paycomonline.net/v4/ats/web.php/portal/"
        f"{PORTAL_KEY}/jobs"
    )
    API_BASE = "https://portal-applicant-tracking.us-cent.paycomonline.net"
    LOCAL_TERMS = ("calaveras lumber", "sonora lumber")

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "CalaverasJobAgent/1.0",
                "Accept": "application/json",
            }
        )
        self._cached_jobs: list[RawJob] | None = None

    @staticmethod
    def _clean_html(value: Any) -> str:
        if not value:
            return ""
        text = html.unescape(str(value))
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
        text = re.sub(r"</(?:p|li|div|h[1-6])>", "\n", text, flags=re.I)
        text = re.sub(r"<li[^>]*>", "- ", text, flags=re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*", "\n", text)
        return text.strip()

    @staticmethod
    def _posted_at(google_job_json: Any) -> datetime | None:
        if not google_job_json:
            return None
        try:
            payload = (
                google_job_json
                if isinstance(google_job_json, dict)
                else json.loads(str(google_job_json))
            )
            value = payload.get("datePosted")
            return datetime.fromisoformat(value) if value else None
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

    @classmethod
    def _is_local_listing(cls, item: dict[str, Any]) -> bool:
        searchable = " ".join(
            str(item.get(field) or "")
            for field in ("jobTitle", "locations", "location", "jobCategory")
        ).casefold()
        return any(term in searchable for term in cls.LOCAL_TERMS)

    def _authorization_headers(self) -> dict[str, str]:
        response = self.session.get(self.PORTAL_URL, timeout=30)
        response.raise_for_status()
        try:
            if response.text.lstrip().startswith("{"):
                config = response.json()
            else:
                match = re.search(
                    r"var\s+configsFromHost\s*=\s*(\{.*?\});",
                    response.text,
                    flags=re.S,
                )
                if not match:
                    raise RuntimeError(
                        "Paycom portal did not provide a session token"
                    )
                config = json.loads(match.group(1))
            token = config["sessionJWT"]
        except RuntimeError:
            raise
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise RuntimeError("Paycom portal returned an invalid session token") from None
        return {
            "Authorization": str(token),
            "Locale": "en-US",
            "Translation-Highlights": "false",
        }

    def _load_previews(self, headers: dict[str, str]) -> list[dict[str, Any]]:
        payload = {
            "skip": 0,
            "take": 250,
            "filtersForQuery": {
                "distanceFrom": 0,
                "workEnvironments": [],
                "positionTypes": [],
                "educationLevels": [],
                "categories": [],
                "travelTypes": [],
                "shiftTypes": [],
                "otherFilters": [],
                "keywordSearchText": "",
                "location": "",
                "sortOption": "N",
            },
        }
        response = self.session.post(
            f"{self.API_BASE}/api/ats/job-posting-previews/search",
            headers=headers,
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        previews = data.get("jobPostingPreviews", []) if isinstance(data, dict) else []
        return [item for item in previews if isinstance(item, dict)]

    def _load_detail(self, job_id: str, headers: dict[str, str]) -> dict[str, Any]:
        response = self.session.get(
            f"{self.API_BASE}/api/ats/job-postings/{job_id}",
            headers=headers,
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        detail = data.get("jobPosting", {}) if isinstance(data, dict) else {}
        return detail if isinstance(detail, dict) else {}

    def _to_job(self, detail: dict[str, Any]) -> RawJob | None:
        job_id = detail.get("jobId")
        title = str(detail.get("jobTitle") or "").strip()
        location = str(detail.get("location") or "").strip()
        if job_id is None or not title or not self._is_local_listing(detail):
            return None

        apply_url = f"{self.APPLY_BASE}/{job_id}"
        qualifications = self._clean_html(detail.get("qualifications"))
        requirements = [qualifications] if qualifications else []
        return RawJob(
            provider_job_id=str(job_id),
            title=title,
            company=self.COMPANY_NAME,
            location=location or "Calaveras County, CA",
            employment_type=(
                str(detail.get("positionType") or "").strip() or None
            ),
            description=self._clean_html(detail.get("description")),
            posted_at=self._posted_at(detail.get("googleJobJson")),
            apply_url=apply_url,
            source=self.SOURCE_KEY,
            source_url=apply_url,
            requirements=requirements,
            metadata={
                "salary": str(detail.get("salaryRange") or "").strip() or None,
                "category": str(detail.get("jobCategory") or "").strip() or None,
                "official_source": True,
                "portal": "Paycom",
            },
        )

    def _load_jobs(self) -> list[RawJob]:
        if self._cached_jobs is not None:
            return self._cached_jobs

        headers = self._authorization_headers()
        jobs: list[RawJob] = []
        seen: set[str] = set()
        for preview in self._load_previews(headers):
            if not self._is_local_listing(preview):
                continue
            job_id = preview.get("jobId")
            if job_id is None or str(job_id) in seen:
                continue
            job = self._to_job(self._load_detail(str(job_id), headers))
            if job:
                seen.add(str(job_id))
                jobs.append(job)
        self._cached_jobs = jobs
        return jobs

    def search(
        self,
        *,
        role: str,
        location: str,
        results_per_page: int = 25,
    ) -> list[RawJob]:
        del role, location
        return self._load_jobs()[: max(1, results_per_page)]
