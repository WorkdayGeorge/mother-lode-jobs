"""Job-source adapters, and recognising a careers URL's platform.

``ADAPTERS`` is the registry the worker and admin screens use.
``detect(url)`` turns a pasted careers-page URL into a suggested adapter and
config so adding an employer is usually: paste, preview, save.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

from .base import Adapter, ConfigError, FetchResult, Http, RawJob, SourceError
from .custom import SCRAPERS, CustomAdapter
from .platforms import (
    AdpAdapter,
    AdzunaAdapter,
    ApplicantProAdapter,
    ApploiAdapter,
    EdJoinAdapter,
    JsonLdAdapter,
    ManualAdapter,
    NeogovAdapter,
    PageWatchAdapter,
)

ADAPTERS: dict[str, type[Adapter]] = {
    cls.key: cls
    for cls in (
        NeogovAdapter,
        EdJoinAdapter,
        AdpAdapter,
        ApplicantProAdapter,
        ApploiAdapter,
        JsonLdAdapter,
        PageWatchAdapter,
        AdzunaAdapter,
        CustomAdapter,
        ManualAdapter,
    )
}

# Adapters an admin can choose when adding a source by hand.
SELECTABLE = [k for k in ADAPTERS if k not in ("manual",)]


def build_adapter(source, http: Http | None = None) -> Adapter:
    cls = ADAPTERS.get(source.adapter)
    if cls is None:
        raise ConfigError(f"Unknown adapter {source.adapter!r}")
    return cls(source.config or {}, source_name=source.name, county=source.county, http=http or Http())


def is_multi_employer(source) -> bool:
    cls = ADAPTERS.get(source.adapter)
    if cls is None:
        return False
    if cls is CustomAdapter:
        from .custom import MULTI_EMPLOYER

        return (source.config or {}).get("scraper") in MULTI_EMPLOYER
    return cls.multi_employer


@dataclass
class Detection:
    adapter: str
    config: dict[str, Any] = field(default_factory=dict)
    note: str = ""
    confident: bool = True


def detect(url: str) -> Detection:
    """Guess the platform behind a careers URL."""
    url = (url or "").strip()
    parsed = urlparse(url if "://" in url else f"https://{url}")
    host = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path
    query = {k: v[0] for k, v in parse_qs(parsed.query).items()}

    if host.endswith("governmentjobs.com"):
        match = re.search(r"/careers/([^/?#]+)", path) or re.search(r"agency=([^&]+)", parsed.query)
        if match:
            return Detection("neogov", {"agency": match.group(1)}, "GovernmentJobs agency page")
        return Detection("neogov", {}, "GovernmentJobs — enter the agency code", confident=False)

    if host.endswith("edjoin.org"):
        district = query.get("districtID") or query.get("districtid")
        if district:
            return Detection("edjoin", {"districts": [{"id": int(district), "name": "", "portal": url}]},
                             "EdJoin district — fill in the district name")
        return Detection("edjoin", {"districts": []},
                         "EdJoin — add the district id (from the district's job list URL, districtID=...)",
                         confident=False)

    if host.endswith("workforcenow.adp.com"):
        cid = query.get("cid")
        config = {"cid": cid} if cid else {}
        if query.get("ccId"):
            config["ccid"] = query["ccId"]
        return Detection("adp", config, "ADP Workforce Now career center", confident=bool(cid))

    if host.endswith("applicantpro.com"):
        sub = host.split(".applicantpro.com")[0]
        config: dict[str, Any] = {"subdomain": sub}
        match = re.search(r"/core/jobs/(\d+)", path)
        if match:
            config["board_id"] = match.group(1)
        return Detection("applicantpro", config, "ApplicantPro job board")

    if host.endswith("apploi.click") or host.endswith("apploi.com"):
        return Detection("apploi", {"url": url}, "Apploi career page")

    if host.endswith("calcareers.ca.gov"):
        return Detection("custom", {"scraper": "calcareers"}, "CalCareers (already covers all three counties)")
    if host.endswith("usajobs.gov"):
        return Detection("custom", {"scraper": "usajobs"}, "USAJOBS")
    if host.endswith("remotive.com"):
        return Detection("custom", {"scraper": "remotive"}, "Remotive")

    known_custom = {
        "commonspirit.careers": "commonspirit",
        "jobs.pge.com": "pge",
        "ccwd.org": "ccwd",
        "careers.travelandleisureco.com": "worldmark_angels_camp",
        "angelscamp.gov": "angels_camp_city",
        "ironstonevineyards.com": "ironstone",
    }
    for suffix, scraper in known_custom.items():
        if host.endswith(suffix):
            return Detection("custom", {"scraper": scraper}, f"Known site: {SCRAPERS[scraper][2]}")

    unsupported = {
        "paycomonline.net": "Paycom",
        "harri.com": "Harri",
        "oraclecloud.com": "Oracle Recruiting",
        "myworkdayjobs.com": "Workday",
        "icims.com": "iCIMS",
        "paylocity.com": "Paylocity",
        "ultipro.com": "UKG",
        "indeed.com": "Indeed",
    }
    for suffix, name in unsupported.items():
        if host.endswith(suffix):
            return Detection(
                "jsonld", {"url": url, "link_pattern": "/job"},
                f"{name} isn't a built-in platform yet. Trying the generic reader; if the preview "
                "finds nothing, use 'Watch a page for changes'.",
                confident=False,
            )

    return Detection(
        "jsonld",
        {"url": url},
        "No known platform. Trying the generic reader (works when the site embeds standard job data). "
        "If the preview finds nothing, switch to 'Watch a page for changes'.",
        confident=False,
    )


__all__ = [
    "ADAPTERS",
    "SELECTABLE",
    "Adapter",
    "ConfigError",
    "Detection",
    "FetchResult",
    "Http",
    "RawJob",
    "SourceError",
    "build_adapter",
    "detect",
    "is_multi_employer",
]
