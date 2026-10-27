from __future__ import annotations

from datetime import datetime, timezone
import html
import re
import time
from typing import Any

import requests

from ._compat import JobProvider
from ._compat import RawJob


class RemotiveProvider(JobProvider):
    """Remote jobs available to applicants living in California."""

    API_URL = "https://remotive.com/api/remote-jobs"
    SOURCE_URL = "https://remotive.com/remote-jobs"

    ELIGIBLE_LOCATION_PATTERNS = (
        r"\bcalifornia\b",
        r"\bunited states\b",
        r"\bunited states of america\b",
        r"\busa\b",
        r"\bu\.s\.\b",
        r"\bus[- ]only\b",
        r"\bnorth america\b",
        r"\bworldwide\b",
        r"\bworld wide\b",
        r"\banywhere\b",
        r"\bglobal\b",
        r"\bamericas\b",
    )

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "CalaverasJobAgent/1.0",
                "Accept": "application/json",
            }
        )
        self._items: list[dict[str, Any]] | None = None

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

    @classmethod
    def _california_eligible(cls, location: Any) -> bool:
        value = re.sub(r"\s+", " ", str(location or "")).strip().lower()
        if not value:
            return False
        return any(re.search(pattern, value) for pattern in cls.ELIGIBLE_LOCATION_PATTERNS)

    @staticmethod
    def _posted_at(value: Any) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    def _to_job(self, item: dict[str, Any]) -> RawJob | None:
        location = str(item.get("candidate_required_location") or "").strip()
        if not self._california_eligible(location):
            return None

        provider_job_id = item.get("id")
        title = str(item.get("title") or "").strip()
        company = str(item.get("company_name") or "").strip()
        url = str(item.get("url") or "").strip()
        if provider_job_id is None or not title or not company or not url:
            return None

        tags = item.get("tags")
        if not isinstance(tags, list):
            tags = []

        salary = str(item.get("salary") or "").strip()
        category = str(item.get("category") or "").strip()
        return RawJob(
            provider_job_id=str(provider_job_id),
            title=title,
            company=company,
            location=f"Remote — {location}",
            employment_type=(
                str(item.get("job_type")).strip() if item.get("job_type") else None
            ),
            description=self._clean_html(item.get("description")),
            posted_at=self._posted_at(item.get("publication_date")),
            # Remotive requires attribution and links back to its listing.
            apply_url=url,
            source="remotive",
            source_url=url,
            requirements=[str(tag).strip() for tag in tags if str(tag).strip()],
            metadata={
                "category": category or None,
                "salary": salary or None,
                "remote_eligibility": "california",
                "candidate_required_location": location,
                "attribution": "Job listing provided by Remotive",
            },
        )

    @staticmethod
    def _matches_role(item: dict[str, Any], role: str) -> bool:
        tokens = re.findall(r"[a-z0-9+#.]+", role.casefold())
        if not tokens:
            return True
        tags = item.get("tags") if isinstance(item.get("tags"), list) else []
        searchable = " ".join(
            [
                str(item.get("title") or ""),
                str(item.get("company_name") or ""),
                str(item.get("category") or ""),
                *(str(tag) for tag in tags),
            ]
        ).casefold()
        return all(token in searchable for token in tokens)

    def _load_items(self) -> list[dict[str, Any]]:
        if self._items is not None:
            return self._items

        retry_statuses = {429, 500, 502, 503, 504}
        response = None

        for attempt in range(3):
            try:
                response = self.session.get(self.API_URL, timeout=30)
            except requests.RequestException:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RuntimeError("Remotive request failed after retries") from None

            if response.status_code in retry_statuses and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            if not response.ok:
                raise RuntimeError(
                    f"Remotive request failed with HTTP {response.status_code}"
                )
            break

        if response is None:
            raise RuntimeError("Remotive request failed after retries")
        try:
            payload = response.json()
        except ValueError:
            raise RuntimeError("Remotive returned invalid JSON") from None

        raw_items = payload.get("jobs", [])
        self._items = [item for item in raw_items if isinstance(item, dict)]
        return self._items

    def search(
        self,
        *,
        role: str,
        location: str,
        results_per_page: int = 25,
    ) -> list[RawJob]:
        del location  # Eligibility comes from each listing's location restriction.

        jobs: list[RawJob] = []
        seen: set[str] = set()
        limit = max(1, results_per_page)
        for item in self._load_items():
            if not self._matches_role(item, role):
                continue
            job = self._to_job(item)
            if not job or not job.provider_job_id or job.provider_job_id in seen:
                continue
            seen.add(job.provider_job_id)
            jobs.append(job)
            if len(jobs) >= limit:
                break
        return jobs
