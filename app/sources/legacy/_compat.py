"""Shim so the one-off scrapers carried over from calaveras_job_agent run unchanged.

Those modules were written against the old ``RawJob`` / ``JobProvider`` /
``env`` names.  Keeping them byte-for-byte (apart from imports) preserves the
parsing behaviour their original tests verified.  New sources should be
written as adapters in ``app/sources/`` instead.
"""
from __future__ import annotations

import os
from abc import ABC
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


def env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    return value if value not in (None, "") else default


@dataclass
class RawJob:
    provider_job_id: str | None
    title: str
    company: str
    location: str | None
    employment_type: str | None
    description: str | None
    posted_at: datetime | None
    apply_url: str
    source: str
    source_url: str | None = None
    requirements: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class JobProvider(ABC):
    def search(self, *, role: str, location: str, results_per_page: int = 25) -> list[RawJob]:
        raise NotImplementedError
