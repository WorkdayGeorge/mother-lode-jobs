from __future__ import annotations

import hashlib
import html
import re
from html.parser import HTMLParser
from urllib.parse import urljoin

import requests

from ._compat import JobProvider
from ._compat import RawJob


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


class _CityJobsParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_jobs_table = False
        self.table_depth = 0
        self.in_row = False
        self.in_cell = False
        self.cell_text: list[str] = []
        self.cell_links: list[str] = []
        self.row: list[dict] = []
        self.rows: list[list[dict]] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "table":
            if "Current Opportunities" in attrs.get("aria-label", ""):
                self.in_jobs_table = True
                self.table_depth = 1
            elif self.in_jobs_table:
                self.table_depth += 1
        elif self.in_jobs_table and tag == "tr":
            self.in_row = True
            self.row = []
        elif self.in_row and tag in {"td", "th"}:
            self.in_cell = True
            self.cell_text = []
            self.cell_links = []
        elif self.in_cell and tag == "a" and attrs.get("href"):
            self.cell_links.append(attrs["href"])

    def handle_endtag(self, tag):
        if tag == "table" and self.in_jobs_table:
            self.table_depth -= 1
            if self.table_depth <= 0:
                self.in_jobs_table = False
        elif self.in_cell and tag in {"td", "th"}:
            self.row.append({
                "text": _clean(" ".join(self.cell_text)),
                "links": list(self.cell_links),
            })
            self.in_cell = False
        elif self.in_row and tag == "tr":
            if self.row:
                self.rows.append(self.row)
            self.in_row = False

    def handle_data(self, data):
        if self.in_cell:
            self.cell_text.append(data)


class _IronstoneJobsParser(HTMLParser):
    START = "available positions"
    STOP = "interested applicants"

    def __init__(self) -> None:
        super().__init__()
        self.capture_tag: str | None = None
        self.capture_text: list[str] = []
        self.started = False
        self.stopped = False
        self.current_title: str | None = None
        self.jobs: list[tuple[str, str]] = []

    def handle_starttag(self, tag, attrs):
        if self.stopped:
            return
        if tag in {"h2", "p"}:
            self.capture_tag = tag
            self.capture_text = []

    def handle_endtag(self, tag):
        if tag != self.capture_tag:
            return
        text = _clean(" ".join(self.capture_text))
        captured_tag = self.capture_tag
        self.capture_tag = None
        if not text:
            return
        lowered = text.casefold()
        if captured_tag == "h2" and lowered == self.START:
            self.started = True
            return
        if self.started and captured_tag == "h2" and lowered == self.STOP:
            self.stopped = True
            return
        if self.started and captured_tag == "h2":
            self.current_title = text
        elif self.started and captured_tag == "p" and self.current_title:
            self.jobs.append((self.current_title, text))
            self.current_title = None

    def handle_data(self, data):
        if self.capture_tag:
            self.capture_text.append(data)


class _CareerPageProvider(JobProvider):
    PAGE_URL = ""

    def __init__(self) -> None:
        self._cached_jobs: list[RawJob] | None = None
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            )
        })

    def search(self, *, role: str, location: str, results_per_page: int = 25):
        if self._cached_jobs is None:
            self._cached_jobs = self._load_jobs()
        return self._cached_jobs[:results_per_page]

    @staticmethod
    def _id(source: str, title: str) -> str:
        value = f"{source}:{title.casefold()}".encode()
        return hashlib.sha256(value).hexdigest()[:24]


class AngelsCampProvider(_CareerPageProvider):
    PAGE_URL = "https://angelscamp.gov/city-hall/human-resources-2/"
    SOURCE_KEY = "angels_camp"

    def _load_jobs(self) -> list[RawJob]:
        response = self.session.get(self.PAGE_URL, timeout=30)
        response.raise_for_status()
        parser = _CityJobsParser()
        parser.feed(response.text)
        jobs = []
        for row in parser.rows:
            if len(row) < 5 or row[0]["text"].casefold() == "job position":
                continue
            title = row[0]["text"]
            if not title:
                continue
            detail_url = next(iter(row[1]["links"]), self.PAGE_URL)
            apply_url = next(iter(row[4]["links"]), detail_url)
            description = "\n\n".join(filter(None, [
                row[1]["text"],
                f"Closing date: {row[3]['text']}" if row[3]["text"] else "",
            ]))
            jobs.append(RawJob(
                provider_job_id=self._id(self.SOURCE_KEY, title),
                title=title,
                company="City of Angels Camp",
                location="Angels Camp, CA / Calaveras County, CA",
                employment_type=None,
                description=description,
                posted_at=None,
                apply_url=urljoin(self.PAGE_URL, apply_url),
                source=self.SOURCE_KEY,
                source_url=urljoin(self.PAGE_URL, detail_url),
                requirements=[],
                metadata={
                    "closing_date": row[3]["text"],
                    "timestamp_note": "The official city page does not publish a verified posting time.",
                },
            ))
        return jobs


class IronstoneProvider(_CareerPageProvider):
    PAGE_URL = "https://ironstonevineyards.com/employment/"
    SOURCE_KEY = "ironstone"

    def _load_jobs(self) -> list[RawJob]:
        response = self.session.get(self.PAGE_URL, timeout=30)
        response.raise_for_status()
        parser = _IronstoneJobsParser()
        parser.feed(response.text)
        jobs = []
        seen = set()
        for title, description in parser.jobs:
            key = title.casefold()
            if key in seen:
                continue
            seen.add(key)
            jobs.append(RawJob(
                provider_job_id=self._id(self.SOURCE_KEY, title),
                title=title,
                company="Ironstone Vineyards",
                location="Murphys, CA / Calaveras County, CA",
                employment_type=None,
                description=description,
                posted_at=None,
                apply_url=self.PAGE_URL,
                source=self.SOURCE_KEY,
                source_url=self.PAGE_URL,
                requirements=[],
                metadata={
                    "timestamp_note": "The official employer page does not publish a verified posting time.",
                },
            ))
        return jobs
