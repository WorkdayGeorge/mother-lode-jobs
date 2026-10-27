"""Adapters for hiring platforms many local employers share.

Each class reads one kind of careers site.  The per-employer details (agency
code, ADP client id, district list...) come from the source row's ``config``,
so a new employer on the same platform needs no new code.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from urllib.parse import urljoin, urlparse

from defusedxml import ElementTree as ET

from app.text import clean_html, squash

from .base import (
    Adapter,
    ConfigField,
    FetchResult,
    RawJob,
    SourceError,
    iter_job_postings,
    parse_date,
    text_or_join,
)

# --------------------------------------------------------------------------
# GovernmentJobs / NEOGOV  (counties, courts, water agencies)
# --------------------------------------------------------------------------


class NeogovAdapter(Adapter):
    key = "neogov"
    label = "GovernmentJobs (NEOGOV)"
    description = "Public agencies whose jobs are at governmentjobs.com/careers/<agency>."
    config_fields = (
        ConfigField("agency", "Agency code", help="The part after /careers/ in the URL, e.g. amadorgov"),
        ConfigField("employer", "Employer name shown on jobs", required=False),
    )

    NS = {"job": "http://www.neogov.com/namespaces/JobListing"}
    FEED = "https://www.governmentjobs.com/SearchEngine/JobsFeed"

    def _t(self, item, name: str) -> str | None:
        el = item.find(name) if ":" not in name else item.find(name, self.NS)
        return el.text.strip() if el is not None and el.text else None

    def fetch(self) -> FetchResult:
        response = self.http.get(self.FEED, params={"agency": self.config["agency"]})
        try:
            root = ET.fromstring(response.content)
        except ET.ParseError as exc:
            raise SourceError("GovernmentJobs feed was not valid XML") from exc

        employer = self.config.get("employer") or self.source_name
        jobs: list[RawJob] = []
        for item in root.findall("./channel/item"):
            link = self._t(item, "link") or ""
            job_id = self._t(item, "job:jobId") or self._t(item, "job:jobNumberSingle") or link
            title = self._t(item, "title")
            if not title or not link:
                continue
            parts = [
                clean_html(self._t(item, "description")),
                clean_html(self._t(item, "job:examplesofduties")),
                clean_html(self._t(item, "job:qualifications")),
                clean_html(self._t(item, "job:supplementalinformation")),
            ]
            low, high = self._t(item, "job:minimumSalary"), self._t(item, "job:maximumSalary")
            interval = self._t(item, "job:salaryInterval")
            salary = None
            if low or high:
                salary = " – ".join(f"${v}" for v in (low, high) if v)
                unit = {"hourly": "hour", "annually": "year", "yearly": "year", "monthly": "month",
                        "biweekly": "two weeks", "daily": "day"}.get((interval or "").lower(), (interval or "").lower())
                if unit:
                    salary += f" per {unit}"
            where = self._t(item, "job:location")
            jobs.append(
                RawJob(
                    external_id=str(job_id),
                    title=title,
                    apply_url=link,
                    employer=employer,
                    location=f"{where}, CA" if where else None,
                    description="\n\n".join(p for p in parts if p),
                    employment_type=self._t(item, "job:jobType"),
                    salary_text=salary,
                    posted_on=parse_date(self._t(item, "job:advertiseFromDateUTC") or self._t(item, "job:advertiseFromDate")),
                    closes_on=parse_date(self._t(item, "job:advertiseToDateTime")),
                    extra={"department": self._t(item, "job:department")},
                )
            )
        return FetchResult(jobs)


# --------------------------------------------------------------------------
# EdJoin  (public school districts)
# --------------------------------------------------------------------------


class EdJoinAdapter(Adapter):
    key = "edjoin"
    label = "EdJoin (school districts)"
    description = "California school districts on edjoin.org. One source can cover a whole county's districts."
    multi_employer = True
    config_fields = (
        ConfigField(
            "districts",
            "Districts",
            kind="districts",
            help="One per line: district ID | district name | portal URL",
        ),
    )

    API = "https://www.edjoin.org/Home/LoadJobsPortalList"

    def fetch(self) -> FetchResult:
        jobs: dict[str, RawJob] = {}
        notes = []
        ok = 0
        for district in self.config["districts"]:
            try:
                for job in self._district(district):
                    jobs[job.external_id] = job
                ok += 1
            except SourceError as exc:
                notes.append(f"{district.get('name')}: {exc}")
        if not ok and notes:
            raise SourceError("; ".join(notes))
        return FetchResult(list(jobs.values()), notes=notes)

    def _district(self, district: dict) -> list[RawJob]:
        out: list[RawJob] = []
        page, pages = 1, 1
        while page <= pages and page <= 20:
            payload = self.http.json(
                self.API,
                params={"rows": 100, "page": page, "catID": 0, "districtID": district["id"]},
                headers={
                    "Referer": district.get("portal") or "https://www.edjoin.org/",
                    "X-Requested-With": "XMLHttpRequest",
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                },
            )
            if page == 1:
                try:
                    pages = max(1, int(payload.get("totalPages") or 1))
                except (TypeError, ValueError):
                    pages = 1
            for item in payload.get("data") or []:
                posting_id, title = item.get("postingID"), squash(item.get("positionTitle"))
                if not posting_id or not title:
                    continue
                city = squash(item.get("city"))
                summary = [squash(item.get("JobSummary")), squash(item.get("postingInformation"))]
                out.append(
                    RawJob(
                        external_id=str(posting_id),
                        title=title,
                        apply_url=f"https://www.edjoin.org/Home/JobPosting/{posting_id}",
                        employer=squash(item.get("districtName")) or district.get("name"),
                        location=f"{city}, CA" if city else None,
                        description="\n\n".join(dict.fromkeys(s for s in summary if s)),
                        employment_type=squash(item.get("FullTimePartTime")) or None,
                        salary_text=squash(item.get("salaryInfo")) or None,
                        posted_on=_ms_date(item.get("postingDate")) or _ms_date(item.get("CreationDate")),
                        closes_on=_ms_date(item.get("displayUntil")),
                        extra={"edjoin_category": item.get("categoryName")},
                    )
                )
            page += 1
        return out


def _ms_date(value: Any):
    """EdJoin dates look like /Date(1767225600000)/."""
    from datetime import datetime, timezone

    match = re.search(r"/Date\((-?\d+)", str(value or ""))
    if not match:
        return parse_date(value)
    try:
        return datetime.fromtimestamp(int(match.group(1)) / 1000, tz=timezone.utc).date()
    except (OverflowError, OSError, ValueError):
        return None


# --------------------------------------------------------------------------
# ADP Workforce Now career center
# --------------------------------------------------------------------------


class AdpAdapter(Adapter):
    key = "adp"
    label = "ADP Workforce Now"
    description = "Careers pages at workforcenow.adp.com/mascsr/... (the cid= value identifies the employer)."
    config_fields = (
        ConfigField("cid", "ADP client id (cid)", help="The cid=... value in the careers URL"),
        ConfigField("ccid", "ADP ccId", required=False, help="Usually 19000101_000001"),
        ConfigField(
            "local_terms",
            "Only keep jobs whose location mentions",
            kind="list",
            required=False,
            help="For employers with sites outside the area, e.g. murphys, calaveras",
        ),
        ConfigField("default_location", "Location when ADP gives none", required=False),
    )

    API = "https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions"
    APPLY = "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html"

    def fetch(self) -> FetchResult:
        cid = self.config["cid"]
        ccid = self.config.get("ccid") or "19000101_000001"
        terms = [t.lower() for t in self.config.get("local_terms") or []]
        headers = {"Accept": "application/json"}

        summaries: dict[str, dict] = {}
        skip, total = 1, None
        while skip <= 1001:
            data = self.http.json(self.API, params={"cid": cid, "$skip": skip, "$top": 20}, headers=headers)
            page = data.get("jobRequisitions") or []
            if total is None:
                total = int((data.get("meta") or {}).get("totalNumber") or 0)
            if not page:
                break
            for item in page:
                if item.get("itemID"):
                    summaries.setdefault(str(item["itemID"]), item)
            if total and len(summaries) >= total:
                break
            skip += 20

        jobs: list[RawJob] = []
        for job_id, summary in summaries.items():
            try:
                detail = self.http.json(f"{self.API}/{job_id}", params={"cid": cid}, headers=headers)
            except SourceError:
                detail = None
            job = detail if isinstance(detail, dict) and detail else summary
            location = self._location(job) or self.config.get("default_location")
            if terms and not any(t in (location or "").lower() for t in terms):
                continue
            apply_url = f"{self.APPLY}?cid={cid}&ccId={ccid}&lang=en_US&jobId={job_id}"
            jobs.append(
                RawJob(
                    external_id=job_id,
                    title=job.get("requisitionTitle") or summary.get("requisitionTitle") or "Untitled role",
                    apply_url=apply_url,
                    location=location,
                    description=clean_html(job.get("requisitionDescription")),
                    employment_type=(job.get("workLevelCode") or {}).get("shortName"),
                    salary_text=self._salary(job),
                    posted_on=parse_date(job.get("postDate") or summary.get("postDate")),
                )
            )
        return FetchResult(jobs)

    @staticmethod
    def _location(job: dict) -> str | None:
        for loc in job.get("requisitionLocations") or []:
            address = loc.get("address") or {}
            city = address.get("cityName")
            state = (address.get("countrySubdivisionLevel1") or {}).get("codeValue")
            if city and state:
                return f"{city}, {state}"
            name = (loc.get("nameCode") or {}).get("shortName")
            if name:
                return name.strip()
        return None

    @staticmethod
    def _salary(job: dict) -> str | None:
        pay = job.get("payGradeRange") or {}
        low = (pay.get("minimumRate") or {}).get("amountValue")
        high = (pay.get("maximumRate") or {}).get("amountValue")
        if low is None and high is None:
            return None
        text = " – ".join(f"${v:g}" for v in (low, high) if v is not None)
        for field in (job.get("customFieldGroup") or {}).get("codeFields") or []:
            if (field.get("nameCode") or {}).get("codeValue") == "SalaryType" and field.get("shortName"):
                text += f" {field['shortName']}"
        return text


# --------------------------------------------------------------------------
# ApplicantPro
# --------------------------------------------------------------------------


class ApplicantProAdapter(Adapter):
    key = "applicantpro"
    label = "ApplicantPro"
    description = "Careers pages at <name>.applicantpro.com/jobs/."
    config_fields = (
        ConfigField("subdomain", "Subdomain", help="The part before .applicantpro.com"),
        ConfigField(
            "board_id",
            "Job board id",
            required=False,
            help="Found automatically when left blank (the number in /core/jobs/<id>)",
        ),
    )

    def _board_id(self) -> str:
        if self.config.get("board_id"):
            return str(self.config["board_id"])
        page = self.http.get(f"https://{self.config['subdomain']}.applicantpro.com/jobs/").text
        match = re.search(r"core/jobs/(\d+)", page) or re.search(r'"domain_?id"\s*:\s*"?(\d+)', page, re.I)
        if not match:
            raise SourceError("Couldn't find the ApplicantPro board id; enter it in the source settings")
        return match.group(1)

    def fetch(self) -> FetchResult:
        base = f"https://{self.config['subdomain']}.applicantpro.com/core/jobs/{self._board_id()}"
        params = {
            "getParams": json.dumps(
                {
                    "cityUrl": "", "countryAbbreviation": "", "stateAbbreviation": "",
                    "isInternal": 0, "showPayFrequency": 1, "showLocation": 1,
                    "showEmploymentType": 1, "showDate": 1,
                },
                separators=(",", ":"),
            )
        }
        payload = self.http.json(base, params=params, headers={"Accept": "application/json"})
        items = (payload.get("data") or {}).get("jobs", []) if payload.get("success") else []
        jobs = []
        for item in items:
            job_id, title, url = str(item.get("id") or ""), squash(item.get("title")), item.get("jobUrl")
            if not (job_id and title and url):
                continue
            try:
                detail = self.http.json(f"{base}/{job_id}/job-details")
                detail = detail.get("data") if detail.get("success") else {}
            except SourceError:
                detail = {}
            detail = detail if isinstance(detail, dict) else {}
            low, high = item.get("minSalary"), item.get("maxSalary")
            salary = " – ".join(f"${v}" for v in (low, high) if v) or None
            if salary and item.get("payTypeFrame"):
                salary += f" {item['payTypeFrame']}"
            jobs.append(
                RawJob(
                    external_id=job_id,
                    title=title,
                    apply_url=url,
                    location=squash(item.get("jobLocation")) or None,
                    description=clean_html(detail.get("advertisingDescriptionHtml") or detail.get("description")),
                    employment_type=text_or_join(item.get("employmentType")),
                    salary_text=salary,
                    posted_on=parse_date(detail.get("startDateRef") or item.get("startDateRef"), "%d-%b-%Y", "%b %d, %Y"),
                    closes_on=parse_date(detail.get("endDateRef") or item.get("endDateRef"), "%d-%b-%Y", "%b %d, %Y"),
                )
            )
        return FetchResult(jobs)


# --------------------------------------------------------------------------
# Apploi
# --------------------------------------------------------------------------


class ApploiAdapter(Adapter):
    key = "apploi"
    label = "Apploi"
    description = "Career pages at apploi.click/<employer>-career-page (common for care centers)."
    config_fields = (ConfigField("url", "Apploi career page URL"),)

    CARD = re.compile(
        r'<div\s+class="jobs-card"[^>]*>.*?<a\s+class="job-link"\s+href="(?P<url>[^"]+)"[^>]*>(?P<title>.*?)</a',
        re.I | re.S,
    )

    def fetch(self) -> FetchResult:
        body = self.http.get(self.config["url"]).text
        jobs, seen = [], set()
        for match in self.CARD.finditer(body):
            url = urljoin(self.config["url"], clean_html(match.group("url")))
            id_match = re.search(r"/job/(\d+)", url)
            if not id_match or id_match.group(1) in seen:
                continue
            seen.add(id_match.group(1))
            posting = {}
            try:
                found = iter_job_postings(self.http.get(url).text)
                posting = found[0] if found else {}
            except SourceError:
                pass
            jobs.append(_from_job_posting(posting, url, id_match.group(1), fallback_title=clean_html(match.group("title"))))
        return FetchResult(jobs)


# --------------------------------------------------------------------------
# Any site that publishes schema.org JobPosting data
# --------------------------------------------------------------------------


class JsonLdAdapter(Adapter):
    key = "jsonld"
    label = "Website with job data (schema.org)"
    description = (
        "Works for many modern careers sites that embed standard job data for Google. "
        "Reads the listing page, and optionally follows links to each job's page."
    )
    config_fields = (
        ConfigField("url", "Careers page URL"),
        ConfigField(
            "link_pattern",
            "Follow links matching (regex)",
            required=False,
            help="e.g. /job/ — leave blank to only read the listing page itself",
        ),
        ConfigField("max_pages", "Max job pages to open", kind="int", required=False),
    )

    def fetch(self) -> FetchResult:
        url = self.config["url"]
        page = self.http.get(url).text
        postings = [(p, url) for p in iter_job_postings(page)]
        pattern = self.config.get("link_pattern")
        if pattern:
            host = urlparse(url).netloc
            links = []
            for href in re.findall(r'href="([^"#]+)"', page):
                full = urljoin(url, clean_html(href))
                if urlparse(full).netloc == host and re.search(pattern, full) and full not in links:
                    links.append(full)
            for link in links[: int(self.config.get("max_pages") or 40)]:
                try:
                    postings.extend((p, link) for p in iter_job_postings(self.http.get(link).text))
                except SourceError:
                    continue
        jobs, seen = [], set()
        for posting, page_url in postings:
            job = _from_job_posting(posting, posting.get("url") or page_url)
            if job.title and job.external_id not in seen:
                seen.add(job.external_id)
                jobs.append(job)
        return FetchResult(jobs)


def _from_job_posting(posting: dict, url: str, external_id: str | None = None, *, fallback_title: str = "") -> RawJob:
    locations = posting.get("jobLocation") or []
    if isinstance(locations, dict):
        locations = [locations]
    location = None
    for loc in locations:
        address = (loc or {}).get("address") or {}
        if isinstance(address, dict):
            location = ", ".join(v for v in (address.get("addressLocality"), address.get("addressRegion")) if v) or None
            if location:
                break
    if (posting.get("jobLocationType") or "").upper() == "TELECOMMUTE":
        location = f"Remote — {text_or_join((posting.get('applicantLocationRequirements') or {}).get('name')) or 'United States'}"
    identifier = posting.get("identifier")
    if isinstance(identifier, dict):
        identifier = identifier.get("value")
    org = posting.get("hiringOrganization")
    salary = None
    base = posting.get("baseSalary")
    if isinstance(base, dict):
        value = base.get("value") or {}
        if isinstance(value, dict):
            low, high = value.get("minValue"), value.get("maxValue") or value.get("value")
            unit = (value.get("unitText") or "").lower()
            if low or high:
                salary = " – ".join(f"${v}" for v in (low, high) if v) + (f" per {unit}" if unit else "")
    return RawJob(
        external_id=str(external_id or identifier or hashlib.sha256(url.encode()).hexdigest()[:24]),
        title=squash(posting.get("title")) or fallback_title,
        apply_url=url,
        employer=squash(org.get("name")) if isinstance(org, dict) else None,
        location=location,
        description=clean_html(posting.get("description")),
        employment_type=text_or_join(posting.get("employmentType")),
        salary_text=salary,
        posted_on=parse_date(posting.get("datePosted")),
        closes_on=parse_date(posting.get("validThrough")),
    )


# --------------------------------------------------------------------------
# Watch a page that has no machine-readable jobs
# --------------------------------------------------------------------------


class PageWatchAdapter(Adapter):
    key = "page_watch"
    label = "Watch a page for changes"
    description = (
        "For small employers whose careers page is just text. Nothing is posted "
        "automatically; you get flagged in Admin when the page changes so you can "
        "add the opening by hand."
    )
    config_fields = (ConfigField("url", "Page URL"),)

    def fetch(self) -> FetchResult:
        text = clean_html(self.http.get(self.config["url"]).text)
        # Ignore things like copyright years and visitor counters.
        text = re.sub(r"\d{1,2}:\d{2}|\b20\d\d\b|\d{3,}", "", text)
        return FetchResult([], page_fingerprint=hashlib.sha256(text.encode()).hexdigest())


# --------------------------------------------------------------------------
# Jobs posted by hand in Admin (or approved employer submissions)
# --------------------------------------------------------------------------


class ManualAdapter(Adapter):
    key = "manual"
    label = "Posted by hand"
    description = "Openings you enter in Admin. The worker leaves these alone; they close on their closing date."
    multi_employer = True

    def fetch(self) -> FetchResult:
        raise SourceError("Manual sources are not fetched")


# --------------------------------------------------------------------------
# Adzuna (aggregator API)
# --------------------------------------------------------------------------


class AdzunaAdapter(Adapter):
    key = "adzuna"
    label = "Adzuna (aggregator)"
    description = "Fills gaps from employers we don't read directly. Needs ADZUNA_APP_ID / ADZUNA_APP_KEY."
    multi_employer = True
    config_fields = (
        ConfigField("where", "Places to search", kind="list", help="One per line, e.g. Calaveras County, CA"),
        ConfigField("max_pages", "Pages per place (50 jobs each)", kind="int", required=False),
    )

    API = "https://api.adzuna.com/v1/api/jobs/us/search/{page}"

    def fetch(self) -> FetchResult:
        from app.config import get_settings

        settings = get_settings()
        if not (settings.adzuna_app_id and settings.adzuna_app_key):
            raise SourceError("ADZUNA_APP_ID and ADZUNA_APP_KEY are not set")
        jobs: dict[str, RawJob] = {}
        for where in self.config["where"]:
            for page in range(1, int(self.config.get("max_pages") or 4) + 1):
                data = self.http.json(
                    self.API.format(page=page),
                    params={
                        "app_id": settings.adzuna_app_id,
                        "app_key": settings.adzuna_app_key,
                        "results_per_page": 50,
                        "where": where,
                        "distance": 5,
                        "sort_by": "date",
                        "max_days_old": 45,
                        "content-type": "application/json",
                    },
                )
                results = data.get("results") or []
                for item in results:
                    if item.get("id") is None or not item.get("redirect_url"):
                        continue
                    low, high = item.get("salary_min"), item.get("salary_max")
                    salary = None
                    if low and str(item.get("salary_is_predicted")) != "1":
                        salary = " – ".join(f"${v:,.0f}" for v in dict.fromkeys((low, high)) if v) + " per year"
                    jobs[str(item["id"])] = RawJob(
                        external_id=str(item["id"]),
                        title=squash(clean_html(item.get("title"))),
                        apply_url=item["redirect_url"],
                        employer=squash((item.get("company") or {}).get("display_name")) or None,
                        location=(item.get("location") or {}).get("display_name"),
                        description=clean_html(item.get("description")),
                        employment_type=item.get("contract_time") or item.get("contract_type"),
                        salary_text=salary,
                        posted_on=parse_date(item.get("created")),
                        attribution="Listing via Adzuna",
                    )
                if len(results) < 50:
                    break
        return FetchResult(list(jobs.values()))
