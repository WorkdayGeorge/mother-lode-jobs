from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from urllib.parse import urljoin

import requests

from ._compat import JobProvider
from ._compat import RawJob


class _InputParser(HTMLParser):
    """Collect input values needed for the ASP.NET postback."""

    def __init__(self):
        super().__init__()
        self.values = {}

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "input":
            return

        a = dict(attrs)
        name = a.get("name")

        if name:
            self.values[name] = a.get("value", "")


class CalCareersProvider(JobProvider):
    BASE_URL = "https://calcareers.ca.gov"

    SEARCH_URL = (
        "https://calcareers.ca.gov/"
        "CalHRPublic/Search/JobSearchResults.aspx"
    )

    # Confirmed from the live CalCareers location selector.
    LOCATION_IDS = {
        "Amador County": 26,
        "Calaveras County": 42,
        "Tuolumne County": 640,
    }

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
    def _field(block: str, css_class: str) -> str | None:
        pattern = (
            rf'<div[^>]*class="[^"]*\b{re.escape(css_class)}\b'
            rf'[^"]*"[^>]*>.*?'
            rf'<div[^>]*class="[^"]*job-details[^"]*"[^>]*>'
            rf'(.*?)</div>'
        )

        match = re.search(
            pattern,
            block,
            flags=re.I | re.S,
        )

        if not match:
            return None

        value = CalCareersProvider._clean_html(match.group(1))
        return value or None

    @staticmethod
    def _working_title(block: str) -> str | None:
        match = re.search(
            r'<div[^>]*class="[^"]*working-title[^"]*"[^>]*>'
            r'.*?<span[^>]*>(.*?)</span>',
            block,
            flags=re.I | re.S,
        )

        if not match:
            return None

        return CalCareersProvider._clean_html(match.group(1))

    @staticmethod
    def _job_control(block: str) -> str | None:
        match = re.search(
            r'Job\s*Control:\s*</div>\s*'
            r'<div[^>]*class="[^"]*job-details[^"]*"[^>]*>'
            r'\s*(\d+)',
            block,
            flags=re.I | re.S,
        )

        if match:
            return match.group(1)

        match = re.search(
            r'JobControlId=(\d+)',
            block,
            flags=re.I,
        )

        return match.group(1) if match else None

    @staticmethod
    def _publish_date(block: str) -> str | None:
        match = re.search(
            r'Publish\s*Date:.*?'
            r'<time[^>]*datetime="([^"]+)"',
            block,
            flags=re.I | re.S,
        )

        return match.group(1).strip() if match else None

    def _build_form(self, page_html: str, location_id: int) -> dict:
        parser = _InputParser()
        parser.feed(page_html)

        v = parser.values

        return {
            "ctl00$ToolkitScriptManager1":
                "ctl00$cphMainContent$ctl00|"
                "ctl00$cphMainContent$btnSearch",

            "__EVENTTARGET":
                "ctl00$cphMainContent$btnSearch",

            "__EVENTARGUMENT": "",

            "__VIEWSTATE":
                v.get("__VIEWSTATE", ""),

            "__VIEWSTATEGENERATOR":
                v.get("__VIEWSTATEGENERATOR", ""),

            "__SCROLLPOSITIONX": "0",
            "__SCROLLPOSITIONY": "0",
            "__VIEWSTATEENCRYPTED": "",

            "__EVENTVALIDATION":
                v.get("__EVENTVALIDATION", ""),

            "ctl00$ucUtilityHeader1$txtGoogleSiteSearch": "",
            "ctl00$hdnShowHeaderPadding": "1",

            "ctl00$cphMainContent$hdnSearchCriteria":
                f"#locid={location_id}",

            "ctl00$cphMainContent$hdnSocMinorCode": "",
            "ctl00$cphMainContent$hdnSocMajorCode": "",
            "ctl00$cphMainContent$hdnClassCertCatIds": "",
            "ctl00$cphMainContent$hdnClassCode": "",
            "ctl00$cphMainContent$hdnJobControlId": "",
            "ctl00$cphMainContent$hdnDepartmentCode": "",
            "ctl00$cphMainContent$hdnInit": "true",
            "ctl00$cphMainContent$hdnResultViewMode": "",
            "ctl00$cphMainContent$hdnEcosProfileSavedSearchId": "",

            "ctl00$cphMainContent$txtKeyword": "",
            "ctl00$cphMainContent$chkExactWordMatch": "on",
            "ctl00$cphMainContent$hdnFilterState": "false",

            "cphMainContent_ddlJobCategories_VI": "",
            "ctl00$cphMainContent$ddlJobCategories": "",
            "ctl00$cphMainContent$ddlJobCategories$DDD$L": "",

            "cphMainContent_ddlLocation_VI": "",
            "ctl00$cphMainContent$ddlLocation": "",
            "ctl00$cphMainContent$ddlLocation$DDD$L": "",

            "cphMainContent_ddlPostedInLast_VI": "",
            "ctl00$cphMainContent$ddlPostedInLast": "",
            "ctl00$cphMainContent$ddlPostedInLast$DDD$L": "",

            "cphMainContent_ddlSalaryRange_VI": "",
            "ctl00$cphMainContent$ddlSalaryRange": "",
            "ctl00$cphMainContent$ddlSalaryRange$DDD$L": "",

            "cphMainContent_ddlDepartment_VI": "",
            "ctl00$cphMainContent$ddlDepartment": "",
            "ctl00$cphMainContent$ddlDepartment$DDD$L": "",

            "cphMainContent_ddlTeleworkType_VI": "",
            "ctl00$cphMainContent$ddlTeleworkType": "",
            "ctl00$cphMainContent$ddlTeleworkType$DDD$L": "",

            "cphMainContent_ddlWorkType_VI": "",
            "ctl00$cphMainContent$ddlWorkType": "",
            "ctl00$cphMainContent$ddlWorkType$DDD$L": "",

            "cphMainContent_ddlWorkSchedlue_VI": "",
            "ctl00$cphMainContent$ddlWorkSchedlue": "",
            "ctl00$cphMainContent$ddlWorkSchedlue$DDD$L": "",

            "cphMainContent_ddlApplicationMethod_VI": "",
            "ctl00$cphMainContent$ddlApplicationMethod": "",
            "ctl00$cphMainContent$ddlApplicationMethod$DDD$L": "",

            "cphMainContent_ddlClassification_VI": "",
            "ctl00$cphMainContent$ddlClassification": "",
            "ctl00$cphMainContent$ddlClassification$DDD$L": "",

            "ctl00$ucSessionTimeoutDialog$tmrCountdown": "1200",

            "__ASYNCPOST": "true",
        }

    def _fetch_search_results(self, location_id: int) -> str:
        response = self.session.get(
            self.SEARCH_URL,
            headers=self.headers,
            timeout=30,
        )
        response.raise_for_status()

        form = self._build_form(response.text, location_id)

        ajax_headers = {
            **self.headers,
            "X-Requested-With": "XMLHttpRequest",
            "X-MicrosoftAjax": "Delta=true",
            "Content-Type":
                "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": self.BASE_URL,
            "Referer":
                f"{self.SEARCH_URL}"
                f"#locid={location_id}",
        }

        response = self.session.post(
            self.SEARCH_URL,
            data=form,
            headers=ajax_headers,
            timeout=30,
        )
        response.raise_for_status()

        return html.unescape(response.text)

    def _fetch_detail(self, job_control: str) -> tuple[str, str]:
        url = (
            f"{self.BASE_URL}/CalHrPublic/Jobs/"
            f"JobPosting.aspx?JobControlId={job_control}"
        )

        try:
            response = self.session.get(
                url,
                headers=self.headers,
                timeout=30,
            )
            response.raise_for_status()

            return self._clean_html(response.text), url

        except requests.RequestException:
            return "", url

    def _parse_jobs(self, text: str, county_name: str) -> list[RawJob]:
        # Locate each result by its JobPosting link.
        link_matches = list(
            re.finditer(
                r'href=["\']'
                r'([^"\']*JobPosting\.aspx\?JobControlId=(\d+)[^"\']*)'
                r'["\']',
                text,
                flags=re.I,
            )
        )

        jobs: list[RawJob] = []
        seen: set[str] = set()

        for index, match in enumerate(link_matches):
            job_control = match.group(2)

            if job_control in seen:
                continue

            seen.add(job_control)

            start = max(
                text.rfind(
                    '<div class="job-result',
                    0,
                    match.start(),
                ),
                text.rfind(
                    '<div class="row',
                    0,
                    match.start(),
                ),
            )

            if start < 0:
                start = max(0, match.start() - 7000)

            if index + 1 < len(link_matches):
                end = link_matches[index + 1].start()
            else:
                end = min(len(text), match.end() + 12000)

            block = text[start:end]

            title = self._working_title(block)

            if not title:
                # Wider fallback around the result.
                wider_start = max(0, match.start() - 8000)
                wider_end = min(len(text), match.end() + 8000)
                block = text[wider_start:wider_end]
                title = self._working_title(block)

            if not title:
                title = f"California State Job {job_control}"

            salary = self._field(block, "salary-range")
            work_type = self._field(block, "schedule")
            department = self._field(block, "department")
            source_location = self._field(block, "location")
            telework = self._field(block, "telework")
            publish_date = self._publish_date(block)

            # Exact county safeguard. The CalCareers query itself is
            # restricted to one requested county, but retain verification.
            if (
                source_location
                and county_name.lower() not in source_location.lower()
            ):
                continue

            location = (
                source_location
                if source_location
                else county_name
            )

            if not re.search(r",\s*CA\s*$", location, re.I):
                location = f"{location}, CA"

            detail_text, apply_url = self._fetch_detail(job_control)

            summary_parts = []

            if department:
                summary_parts.append(f"Department: {department}")

            if salary:
                summary_parts.append(f"Salary: {salary}")

            if work_type:
                summary_parts.append(
                    f"Work Type/Schedule: {work_type}"
                )

            if telework:
                summary_parts.append(f"Telework: {telework}")

            if detail_text:
                summary_parts.append(detail_text)

            description = "\n\n".join(summary_parts)

            # IMPORTANT:
            #
            # CalCareers exposes a field labelled Publish Date, but our
            # live testing returned suspicious date values. Until that
            # field is independently verified as the exact original
            # posting timestamp, it must NOT be used for the strict
            # <=60-minute alert.
            #
            # Therefore posted_at deliberately remains None.

            jobs.append(
                RawJob(
                    provider_job_id=job_control,
                    title=title,
                    company=department or "State of California",
                    location=location,
                    employment_type=work_type,
                    description=description,
                    posted_at=None,
                    apply_url=apply_url,
                    source="calcareers",
                    source_url=apply_url,
                    requirements=[],
                    metadata={
                        "job_control": job_control,
                        "department": department,
                        "salary": salary,
                        "work_type": work_type,
                        "telework": telework,
                        "calcareers_publish_date": publish_date,
                        "timestamp_note": (
                            "CalCareers Publish Date retained as "
                            "metadata only. Exact original posting "
                            "timestamp is not verified."
                        ),
                    },
                )
            )

        return jobs

    def _load_jobs(self) -> list[RawJob]:
        jobs: dict[str, RawJob] = {}
        successful_counties = 0
        last_error: requests.RequestException | None = None
        for county_name, location_id in self.LOCATION_IDS.items():
            try:
                text = self._fetch_search_results(location_id)
                successful_counties += 1
            except requests.RequestException as exc:
                # A single county failure must not discard healthy counties.
                last_error = exc
                continue
            for job in self._parse_jobs(text, county_name):
                jobs[job.provider_job_id or job.apply_url] = job
        if successful_counties == 0 and last_error is not None:
            raise RuntimeError(
                "CalCareers search failed for all configured counties"
            ) from last_error
        return list(jobs.values())

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
