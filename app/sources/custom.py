"""Adapters for sites that need their own scraper.

These wrap the site-specific scrapers carried over from calaveras_job_agent
(``app/sources/legacy``).  Each is pinned to one website, so a source row
picks one with ``adapter = "custom"`` and ``config = {"scraper": "<name>"}``.
"""
from __future__ import annotations

import re
from importlib import import_module
from typing import Any, Callable

from app.text import clean_html

from .base import Adapter, ConfigField, FetchResult, RawJob, SourceError, parse_date
from .legacy._compat import RawJob as LegacyJob

# name -> (module, class, label, multi_employer)
SCRAPERS: dict[str, tuple[str, str, str]] = {
    "calcareers": ("calcareers", "CalCareersProvider", "CalCareers (State of California) — 3 counties"),
    "usajobs": ("usajobs", "USAJobsProvider", "USAJOBS federal jobs (needs USAJOBS_API_KEY)"),
    "remotive": ("remotive", "RemotiveProvider", "Remotive remote jobs open to Californians"),
    "commonspirit": ("commonspirit", "CommonSpiritProvider", "CommonSpirit / Mark Twain Medical Center"),
    "adventist_health": ("adventist_health", "AdventistHealthProvider", "Adventist Health Sonora (Oracle)"),
    "pge": ("pge", "PGEProvider", "PG&E foothill jobs"),
    "ccwd": ("ccwd", "CCWDProvider", "Calaveras County Water District site"),
    "greenhorn_creek": ("greenhorn_creek", "GreenhornCreekProvider", "Greenhorn Creek Resort (Harri)"),
    "calaveras_lumber": ("calaveras_lumber", "CalaverasLumberProvider", "Calaveras & Sonora Lumber (Paycom)"),
    "worldmark_angels_camp": ("worldmark_angels_camp", "WorldMarkAngelsCampProvider", "WorldMark Angels Camp"),
    "angels_camp_city": ("local_career_pages", "AngelsCampProvider", "City of Angels Camp HR page"),
    "ironstone": ("local_career_pages", "IronstoneProvider", "Ironstone Vineyards employment page"),
}

MULTI_EMPLOYER = {"calcareers", "usajobs", "remotive", "pge"}

# Settings a source row may override on a scraper (attribute name -> caster).
OVERRIDES: dict[str, dict[str, Callable[[Any], Any]]] = {
    "usajobs": {"organization": str, "location": str, "radius_miles": int},
}


class CustomAdapter(Adapter):
    key = "custom"
    label = "Site-specific scraper"
    description = "A scraper written for one particular website."
    config_fields = (ConfigField("scraper", "Scraper", kind="choice"),)

    def __init__(self, config, **kwargs):
        super().__init__(config, **kwargs)
        name = self.config["scraper"]
        if name not in SCRAPERS:
            raise SourceError(f"Unknown scraper {name!r}")
        self.name = name
        self.multi_employer = name in MULTI_EMPLOYER  # type: ignore[misc]

    def _provider(self):
        module, cls, _label = SCRAPERS[self.name]
        provider = getattr(import_module(f"app.sources.legacy.{module}"), cls)()
        for attr, cast in OVERRIDES.get(self.name, {}).items():
            if attr in self.config and self.config[attr] not in (None, ""):
                setattr(provider, attr, cast(self.config[attr]))
        if self.name == "usajobs" and not getattr(provider, "organization", None):
            provider.organization = ""  # all agencies
        return provider

    def fetch(self) -> FetchResult:
        provider = self._provider()
        try:
            if self.name == "remotive":
                items = provider._load_items()
                legacy = [job for job in (provider._to_job(i) for i in items) if job]
                limit = int(self.config.get("max_jobs") or 300)
                legacy = legacy[:limit]
            else:
                legacy = provider._load_jobs()
        except SourceError:
            raise
        except Exception as exc:  # legacy scrapers raise requests/runtime errors
            message = str(exc)
            if "Max retries exceeded" in message or "ConnectionPool" in message:
                host = re.search(r"host='([^']+)'", message)
                message = f"Couldn't reach {host.group(1) if host else 'the site'}"
            raise SourceError(f"{type(exc).__name__}: {message[:300]}") from exc
        return FetchResult([convert(job) for job in legacy if job and job.title and job.apply_url])


def convert(job: LegacyJob) -> RawJob:
    meta = job.metadata or {}
    posted = job.posted_at.date() if job.posted_at else None
    if posted is None:
        for key in ("posting_date", "publication_start_date", "advertise_from_utc", "date_posted", "posted_date"):
            posted = parse_date(meta.get(key))
            if posted:
                break
    closes = None
    for key in ("closing_date", "application_close_date", "deadline", "final_filing_date"):
        closes = parse_date(meta.get(key))
        if closes:
            break
    description = job.description or ""
    if job.requirements and not any(r in description for r in job.requirements[:1]):
        description = "\n\n".join([description, *job.requirements]).strip()
    return RawJob(
        external_id=str(job.provider_job_id or job.apply_url),
        title=job.title,
        apply_url=job.apply_url,
        employer=job.company or None,
        location=job.location,
        description=clean_html(description) if "<" in description else description,
        employment_type=job.employment_type,
        salary_text=meta.get("salary") or None,
        posted_on=posted,
        closes_on=closes,
        attribution=meta.get("attribution"),
        extra={k: v for k, v in meta.items() if k in ("department", "job_categories", "who_may_apply")},
    )
