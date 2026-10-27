from __future__ import annotations

import math
from datetime import datetime
from typing import Any

import requests

from ._compat import JobProvider
from ._compat import env
from ._compat import RawJob


class USAJobsProvider(JobProvider):
    """Reusable federal job provider backed by the official USAJOBS API."""

    SEARCH_URL = "https://data.usajobs.gov/api/search"
    DEFAULT_ORGANIZATION = "AG11"  # U.S. Forest Service
    DEFAULT_LOCATION = "Sonora, California"
    DEFAULT_RADIUS_MILES = 75
    CENTER_LATITUDE = 38.1960
    CENTER_LONGITUDE = -120.6805

    LOCALITIES = {
        "angels camp",
        "arnold",
        "auburn",
        "avery",
        "camino",
        "copperopolis",
        "groveland",
        "hathaway pines",
        "ione",
        "jackson",
        "jamestown",
        "mi-wuk village",
        "mokelumne hill",
        "murphys",
        "pinecrest",
        "placerville",
        "san andreas",
        "sonora",
        "sutter creek",
        "twain harte",
        "valley springs",
    }

    def __init__(self) -> None:
        self.api_key = env("USAJOBS_API_KEY")
        self.user_agent = env("USAJOBS_USER_AGENT")
        self.organization = (
            env("USAJOBS_ORGANIZATION", self.DEFAULT_ORGANIZATION)
            or self.DEFAULT_ORGANIZATION
        )
        self.location = (
            env("USAJOBS_LOCATION", self.DEFAULT_LOCATION)
            or self.DEFAULT_LOCATION
        )
        try:
            self.radius_miles = int(
                env("USAJOBS_RADIUS", str(self.DEFAULT_RADIUS_MILES))
                or self.DEFAULT_RADIUS_MILES
            )
        except ValueError:
            self.radius_miles = self.DEFAULT_RADIUS_MILES
        self.session = requests.Session()
        self._jobs: list[RawJob] | None = None

    def _headers(self) -> dict[str, str]:
        if not self.api_key or not self.user_agent:
            raise RuntimeError(
                "USAJOBS_API_KEY and USAJOBS_USER_AGENT must be configured"
            )
        return {
            "Host": "data.usajobs.gov",
            "User-Agent": self.user_agent,
            "Authorization-Key": self.api_key,
            "Accept": "application/json",
        }

    @staticmethod
    def _parse_timestamp(value: Any) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        # USAJOBS frequently represents a date as midnight. Do not claim that
        # placeholder is the exact instant when the announcement was posted.
        if parsed.tzinfo is None or (
            parsed.hour == 0 and parsed.minute == 0 and parsed.second == 0
        ):
            return None
        return parsed

    @classmethod
    def _distance_miles(cls, latitude: Any, longitude: Any) -> float | None:
        try:
            lat = math.radians(float(latitude))
            lon = math.radians(float(longitude))
        except (TypeError, ValueError):
            return None
        center_lat = math.radians(cls.CENTER_LATITUDE)
        center_lon = math.radians(cls.CENTER_LONGITUDE)
        delta_lat = lat - center_lat
        delta_lon = lon - center_lon
        value = (
            math.sin(delta_lat / 2) ** 2
            + math.cos(center_lat)
            * math.cos(lat)
            * math.sin(delta_lon / 2) ** 2
        )
        return 3958.8 * 2 * math.asin(min(1.0, math.sqrt(value)))

    def _local_location(self, descriptor: dict) -> str | None:
        locations = descriptor.get("PositionLocation") or []
        for place in locations:
            if not isinstance(place, dict):
                continue
            name = str(
                place.get("LocationName") or place.get("CityName") or ""
            ).strip()
            lower_name = name.lower()
            distance = self._distance_miles(
                place.get("Latitude"), place.get("Longitude")
            )
            if any(locality in lower_name for locality in self.LOCALITIES) or (
                distance is not None and distance <= self.radius_miles
            ):
                return name
        display = str(descriptor.get("PositionLocationDisplay") or "").strip()
        if any(locality in display.lower() for locality in self.LOCALITIES):
            return display
        return None

    @staticmethod
    def _names(values: Any) -> str | None:
        if not isinstance(values, list):
            return None
        names = [str(item.get("Name")) for item in values if item.get("Name")]
        return ", ".join(names) or None

    def _to_job(self, item: dict) -> RawJob | None:
        descriptor = item.get("MatchedObjectDescriptor") or {}
        if not isinstance(descriptor, dict):
            return None
        location = self._local_location(descriptor)
        if not location:
            return None

        job_id = str(
            descriptor.get("PositionID") or item.get("MatchedObjectId") or ""
        ).strip()
        title = str(descriptor.get("PositionTitle") or "").strip()
        position_url = str(descriptor.get("PositionURI") or "").strip()
        if not job_id or not title or not position_url:
            return None

        details = (descriptor.get("UserArea") or {}).get("Details") or {}
        description_parts = [
            details.get("JobSummary"),
            details.get("MajorDuties"),
            descriptor.get("QualificationSummary"),
        ]
        description = "\n\n".join(
            str(value).strip() for value in description_parts if value
        )
        requirements = [
            str(value).strip()
            for value in (
                details.get("Requirements"),
                details.get("Education"),
                details.get("Evaluations"),
            )
            if value
        ]
        publication_start = descriptor.get("PublicationStartDate")
        posted_at = self._parse_timestamp(publication_start)

        return RawJob(
            provider_job_id=job_id,
            title=title,
            company=str(
                details.get("SubAgencyName")
                or descriptor.get("OrganizationName")
                or descriptor.get("DepartmentName")
                or "U.S. Federal Government"
            ),
            location=location,
            employment_type=self._names(descriptor.get("PositionSchedule")),
            description=description,
            posted_at=posted_at,
            apply_url=position_url,
            source="usajobs",
            source_url="https://www.usajobs.gov/",
            requirements=requirements,
            metadata={
                "control_number": item.get("MatchedObjectId"),
                "department": descriptor.get("DepartmentName"),
                "organization": descriptor.get("OrganizationName"),
                "organization_codes": details.get("OrganizationCodes"),
                "job_categories": self._names(descriptor.get("JobCategory")),
                "grades": [
                    grade.get("Code")
                    for grade in descriptor.get("JobGrade") or []
                    if isinstance(grade, dict) and grade.get("Code")
                ],
                "publication_start_date": publication_start,
                "application_close_date": descriptor.get(
                    "ApplicationCloseDate"
                ),
                "who_may_apply": (
                    details.get("WhoMayApply") or {}
                ).get("Name"),
                "timestamp_note": (
                    "Verified exact timestamp from USAJOBS PublicationStartDate."
                    if posted_at
                    else "USAJOBS supplied only a date or midnight placeholder; "
                    "the exact posting timestamp is unverified."
                ),
            },
        )

    def _search_page(self, page: int) -> dict:
        response = self.session.get(
            self.SEARCH_URL,
            headers=self._headers(),
            params={
                "Organization": self.organization,
                "LocationName": self.location,
                "Radius": self.radius_miles,
                "WhoMayApply": "public",
                "DatePosted": 60,
                "Fields": "Full",
                "ResultsPerPage": 100,
                "Page": page,
                "SortField": "opendate",
                "SortDirection": "Desc",
            },
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def _load_jobs(self) -> list[RawJob]:
        if self._jobs is not None:
            return self._jobs

        jobs: dict[str, RawJob] = {}
        page = 1
        while True:
            payload = self._search_page(page)
            search_result = payload.get("SearchResult") or {}
            rows = search_result.get("SearchResultItems") or []
            for row in rows:
                job = self._to_job(row)
                if job:
                    jobs[job.provider_job_id or job.apply_url] = job

            user_area = search_result.get("UserArea") or {}
            try:
                pages = int(user_area.get("NumberOfPages") or 1)
            except (TypeError, ValueError):
                pages = 1
            if page >= pages or not rows:
                break
            page += 1

        self._jobs = list(jobs.values())
        return self._jobs

    def search(
        self,
        *,
        role: str,
        location: str,
        results_per_page: int = 25,
    ) -> list[RawJob]:
        # API-level agency/geographic filters define this small pool; the
        # existing evaluator handles candidate relevance consistently.
        return self._load_jobs()[:results_per_page]
