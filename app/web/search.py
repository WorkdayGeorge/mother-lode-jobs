"""Job search shared by the public board, town/employer pages and alerts preview."""
from __future__ import annotations

from app.clock import local_today
import re
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import Job
from app.region import COUNTIES, TOWN_SLUGS
from app.text import slugify

PAGE_SIZE = 25
COUNTY_SLUGS = {slugify(name.replace(" County", "")): name for name in COUNTIES}
POSTED_CHOICES = {"1": "Today", "3": "Last 3 days", "7": "This week", "30": "This month"}


@dataclass
class Filters:
    q: str = ""
    county: str = ""      # slug, e.g. "calaveras"
    town: str = ""        # slug, e.g. "angels-camp"
    category: str = ""
    type: str = ""
    posted: str = ""
    remote: bool = False
    employer: str = ""    # slug
    page: int = 1

    @classmethod
    def from_query(cls, params) -> "Filters":
        def get(name: str) -> str:
            return (params.get(name) or "").strip()[:100]

        try:
            page = max(1, min(400, int(get("page") or 1)))
        except ValueError:
            page = 1
        return cls(
            q=get("q"), county=get("county"), town=get("town"), category=get("category"),
            type=get("type"), posted=get("posted") if get("posted") in POSTED_CHOICES else "",
            remote=get("remote") in ("1", "on", "true"), employer=get("employer"), page=page,
        )

    def query_string(self, **overrides) -> str:
        from urllib.parse import urlencode

        values = {**self.__dict__, **overrides}
        pairs = {k: v for k, v in values.items() if v not in ("", False, None, 1) or (k == "page" and v != 1)}
        if pairs.get("remote") is True:
            pairs["remote"] = "1"
        return urlencode(pairs)

    @property
    def county_name(self) -> str | None:
        return COUNTY_SLUGS.get(self.county)

    @property
    def town_name(self) -> str | None:
        return TOWN_SLUGS.get(self.town)

    @property
    def is_empty(self) -> bool:
        return not any((self.q, self.county, self.town, self.category, self.type, self.posted, self.remote, self.employer))

    @property
    def active_labels(self) -> list[tuple[str, str]]:
        """(label, query string without it) for the 'remove filter' chips."""
        out = []
        if self.q:
            out.append((f"“{self.q}”", self.query_string(q="", page=1)))
        if self.town_name:
            out.append((self.town_name, self.query_string(town="", page=1)))
        elif self.county_name:
            out.append((self.county_name, self.query_string(county="", page=1)))
        if self.category:
            out.append((self.category, self.query_string(category="", page=1)))
        if self.type:
            out.append((self.type, self.query_string(type="", page=1)))
        if self.posted:
            out.append((POSTED_CHOICES[self.posted], self.query_string(posted="", page=1)))
        if self.remote:
            out.append(("Including remote", self.query_string(remote=False, page=1)))
        return out


def _words(q: str) -> list[str]:
    return [w for w in re.findall(r"[\w&+#-]+", q.lower()) if len(w) > 1][:6]


def base_query(filters: Filters):
    query = select(Job).where(Job.status == "open")
    for word in _words(filters.q):
        like = f"%{word}%"
        query = query.where(or_(Job.title.ilike(like), Job.employer.ilike(like), Job.description.ilike(like)))
    place = None
    if filters.town_name:
        place = Job.town == filters.town_name
    elif filters.county_name:
        place = Job.county == filters.county_name
    if filters.remote:
        if place is not None:
            query = query.where(or_(place, Job.is_remote.is_(True)))
    else:
        query = query.where(Job.is_remote.is_(False))
        if place is not None:
            query = query.where(place)
    if filters.category:
        query = query.where(Job.category == filters.category)
    if filters.type:
        query = query.where(Job.employment_type == filters.type)
    if filters.posted:
        query = query.where(Job.listed_on >= local_today() - timedelta(days=int(filters.posted) - 1))
    if filters.employer:
        query = query.where(Job.employer_slug == filters.employer)
    return query


def search(session: Session, filters: Filters, page_size: int = PAGE_SIZE) -> tuple[list[Job], int]:
    query = base_query(filters)
    total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = session.scalars(
        query.order_by(Job.listed_on.desc(), Job.first_seen_at.desc(), Job.id.desc())
        .offset((filters.page - 1) * page_size)
        .limit(page_size)
    ).all()
    return list(rows), total


def facet_counts(session: Session, filters: Filters) -> dict[str, dict[str, int]]:
    """Counts for the filter menus, ignoring that filter itself."""
    from dataclasses import replace

    out: dict[str, dict[str, int]] = {}
    for column, name, cleared in (
        (Job.category, "category", replace(filters, category="", page=1)),
        (Job.county, "county", replace(filters, county="", town="", page=1)),
        (Job.employment_type, "type", replace(filters, type="", page=1)),
    ):
        sub = base_query(cleared).subquery()
        rows = session.execute(select(sub.c[column.key], func.count()).group_by(sub.c[column.key])).all()
        out[name] = {key: count for key, count in rows if key}
    return out
