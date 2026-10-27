from __future__ import annotations

import hashlib
import re
from html.parser import HTMLParser
from urllib.parse import urljoin

import requests

from ._compat import JobProvider, RawJob


class _CCWDParser(HTMLParser):
    """Extract headings, text, and links from the CCWD jobs page."""

    def __init__(self):
        super().__init__()
        self.sections = []
        self.current = None
        self.in_heading = False
        self.heading_parts = []
        self.current_link = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)

        if tag == "h3":
            self.in_heading = True
            self.heading_parts = []

        elif tag == "a" and self.current is not None:
            href = attrs.get("href")
            if href:
                self.current_link = {
                    "href": href,
                    "parts": [],
                }

    def handle_data(self, data):
        text = data.strip()
        if not text:
            return

        if self.in_heading:
            self.heading_parts.append(text)
        elif self.current_link is not None:
            self.current_link["parts"].append(text)
            self.current["text"].append(text)
        elif self.current is not None:
            self.current["text"].append(text)

    def handle_endtag(self, tag):
        if tag == "h3" and self.in_heading:
            title = " ".join(self.heading_parts).strip()

            if title:
                self.current = {
                    "title": title,
                    "text": [],
                    "links": [],
                }
                self.sections.append(self.current)

            self.in_heading = False
            self.heading_parts = []

        elif tag == "a" and self.current_link is not None:
            self.current_link["label"] = " ".join(
                self.current_link["parts"]
            ).strip()

            self.current["links"].append(self.current_link)
            self.current_link = None


class CCWDProvider(JobProvider):
    """Calaveras County Water District direct-employer provider."""

    JOBS_URL = "https://www.ccwd.org/job-opportunities"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (compatible; CalaverasJobAgent/1.0)"
                )
            }
        )
        self._jobs = None

    @staticmethod
    def _clean(value):
        return re.sub(r"\s+", " ", value or "").strip()

    @staticmethod
    def _job_id(title, apply_url):
        value = f"{title}|{apply_url}"
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]

    def _load_jobs(self):
        if self._jobs is not None:
            return self._jobs

        response = self.session.get(self.JOBS_URL, timeout=30)
        response.raise_for_status()

        parser = _CCWDParser()
        parser.feed(response.text)

        jobs = []

        ignored_headings = {
            "job opportunities",
            "sign up for updates from calaveras county water district",
        }

        for section in parser.sections:
            title = self._clean(section["title"])

            if not title or title.lower() in ignored_headings:
                continue

            description = self._clean(" ".join(section["text"]))

            links = []
            for link in section["links"]:
                href = urljoin(self.JOBS_URL, link["href"])
                label = self._clean(link.get("label", ""))

                links.append(
                    {
                        "label": label,
                        "url": href,
                    }
                )

            # Keep only genuine job-posting sections.
            labels = " ".join(link["label"].lower() for link in links)
            urls = " ".join(link["url"].lower() for link in links)

            is_job_section = (
                "app.smartsheet.com" in urls
                or (
                    "job post" in labels
                    and "employment application" in labels
                )
            )

            if not is_job_section:
                continue

            apply_url = self.JOBS_URL

            # Prefer an explicit application link.
            for link in links:
                label = link["label"].lower()

                if "apply" in label or "click here" in label:
                    apply_url = link["url"]
                    break

            # If there was no obvious application link, use the first
            # useful link associated with the opening.
            if apply_url == self.JOBS_URL and links:
                apply_url = links[0]["url"]

            jobs.append(
                RawJob(
                    provider_job_id=self._job_id(title, apply_url),
                    title=title,
                    company="Calaveras County Water District",
                    location="San Andreas, CA / Calaveras County, CA",
                    employment_type=None,
                    description=description,
                    posted_at=None,
                    apply_url=apply_url,
                    source="ccwd",
                    source_url=self.JOBS_URL,
                    requirements=[],
                    metadata={
                        "timestamp_note": (
                            "CCWD page does not provide a trustworthy "
                            "exact posting timestamp."
                        ),
                        "links": links,
                    },
                )
            )

        self._jobs = jobs
        return jobs

    def search(self, role=None, location=None, **kwargs):
        jobs = self._load_jobs()

        tokens = [
            token
            for token in re.findall(r"[a-z0-9]+", (role or "").lower())
            if len(token) >= 3
        ]

        if not tokens:
            return jobs

        matches = []

        for job in jobs:
            haystack = f"{job.title} {job.description or ''}".lower()

            if any(token in haystack for token in tokens):
                matches.append(job)

        return matches
