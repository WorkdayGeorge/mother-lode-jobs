"""The worker: read every source once, update the job list, then match and alert.

Run with ``python -m app.cli run`` (Cloud Scheduler calls this every 15
minutes in production).
"""
from __future__ import annotations

from .clock import local_date, local_today
import hashlib
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .categories import categorize, employment_type, work_arrangement
from .models import Job, Source, SourceRun, WorkerRun, utcnow
from .region import locate
from .sources import FetchResult, Http, RawJob, SourceError, build_adapter, is_multi_employer
from .text import clean_html, parse_salary, slugify, squash

log = logging.getLogger(__name__)

FETCH_THREADS = 6


# --------------------------------------------------------------------------
# normalising one raw job
# --------------------------------------------------------------------------


def normalize(raw: RawJob, source: Source) -> dict | None:
    """Turn an adapter's RawJob into Job column values, or None if it's out of the area."""
    title = squash(raw.title)
    if not title or not raw.apply_url:
        return None

    multi = is_multi_employer(source)
    employer = squash(raw.employer) if (multi and raw.employer) else source.name
    if not multi and (source.config or {}).get("employer"):
        employer = source.config["employer"]

    place = locate(raw.location, county_hint=source.county)
    if source.kind == "remote":
        if not place.is_remote:
            return None
    elif not place.in_region:
        return None

    description = raw.description or ""
    if "<" in description and ">" in description:
        description = clean_html(description)
    description = description.strip()[:20000] or None

    low, high, unit = parse_salary(raw.salary_text)
    category = categorize(title, description, hint=source.default_category)
    arrangement = "remote" if place.is_remote else work_arrangement(title, raw.location, description)

    values = dict(
        external_id=str(raw.external_id)[:300],
        title=title[:300],
        slug=slugify(f"{title} {employer}", 110),
        employer=employer[:200],
        employer_slug=slugify(employer, 110),
        location=squash(raw.location)[:300] or (place.town and f"{place.town}, CA") or place.county,
        town=place.town,
        county=place.county,
        is_remote=place.is_remote,
        work_arrangement=arrangement,
        category=category,
        employment_type=employment_type(raw.employment_type, title),
        salary_text=squash(raw.salary_text)[:200] or None,
        salary_min=low,
        salary_max=high,
        salary_unit=unit,
        description=description,
        apply_url=raw.apply_url[:2000],
        posted_on=raw.posted_on if raw.posted_on and raw.posted_on <= local_today() + timedelta(days=1) else None,
        closes_on=raw.closes_on,
        attribution=raw.attribution,
        extra=raw.extra or {},
    )
    values["content_hash"] = hashlib.sha256(
        "|".join(str(values[k]) for k in ("title", "location", "description", "salary_text", "closes_on")).encode()
    ).hexdigest()
    return values


# --------------------------------------------------------------------------
# applying one source's results
# --------------------------------------------------------------------------


def apply_result(session: Session, source: Source, result: FetchResult, run: SourceRun) -> None:
    now = utcnow()
    run.jobs_found = len(result.jobs)
    run.jobs_kept = run.jobs_new = run.jobs_closed = 0

    if source.adapter == "page_watch":
        if result.page_fingerprint and result.page_fingerprint != source.page_fingerprint:
            if source.page_fingerprint is not None:
                source.page_changed_at = now
            source.page_fingerprint = result.page_fingerprint
        _mark_success(source, 0, now)
        return

    existing = {
        job.external_id: job
        for job in session.scalars(select(Job).where(Job.source_id == source.id))
    }
    seen: set[str] = set()
    for raw in result.jobs:
        values = normalize(raw, source)
        if values is None or values["external_id"] in seen:
            continue
        seen.add(values["external_id"])
        run.jobs_kept += 1
        job = existing.get(values["external_id"])
        if job is None:
            listed = values["posted_on"] if values["posted_on"] and (local_date(now) - values["posted_on"]).days <= 60 else local_date(now)
            job = Job(source_id=source.id, first_seen_at=now, last_seen_at=now, status="open", listed_on=listed, **values)
            session.add(job)
            run.jobs_new += 1
        else:
            for key, value in values.items():
                setattr(job, key, value)
            job.last_seen_at = now
            if job.status == "closed":
                job.status, job.closed_at = "open", None

    # Close jobs that disappeared — but a source that suddenly returns nothing
    # is more likely broken than empty, so wait for a second empty run.
    suspicious_empty = run.jobs_kept == 0 and (source.max_job_count or 0) > 0 and (source.consecutive_empty or 0) < 1
    if not suspicious_empty:
        for external_id, job in existing.items():
            if job.status == "open" and external_id not in seen:
                job.status, job.closed_at = "closed", now
                run.jobs_closed += 1

    _mark_success(source, run.jobs_kept, now)


def _mark_success(source: Source, count: int, now: datetime) -> None:
    source.last_run_at = now
    source.last_success_at = now
    source.last_error = None
    source.consecutive_failures = 0
    source.consecutive_empty = (source.consecutive_empty or 0) + 1 if count == 0 else 0
    source.last_job_count = count
    source.max_job_count = max(source.max_job_count or 0, count)


def _mark_failure(source: Source, error: str, now: datetime) -> None:
    source.last_run_at = now
    source.last_error = error[:2000]
    source.consecutive_failures = (source.consecutive_failures or 0) + 1


def _fetch(source: Source) -> FetchResult | SourceError:
    try:
        return build_adapter(source, Http()).fetch()
    except SourceError as exc:
        return exc
    except Exception as exc:  # never let one bad source stop the run
        log.exception("Source %s crashed", source.key)
        return SourceError(f"{type(exc).__name__}: {exc}")


def run_sources(session: Session, *, keys: list[str] | None = None) -> dict:
    query = select(Source).where(Source.adapter != "manual")
    if keys:
        query = query.where(Source.key.in_(keys))
    else:
        query = query.where(Source.active.is_(True))
    sources = list(session.scalars(query.order_by(Source.id)))

    with ThreadPoolExecutor(max_workers=FETCH_THREADS) as pool:
        results = dict(zip([s.id for s in sources], pool.map(_fetch, sources)))

    summary = {"sources": len(sources), "ok": 0, "failed": 0, "new_jobs": 0, "closed_jobs": 0, "errors": {}}
    for source in sources:
        run = SourceRun(source_id=source.id, started_at=utcnow())
        session.add(run)
        result = results[source.id]
        if isinstance(result, SourceError):
            run.ok, run.error = False, str(result)
            _mark_failure(source, str(result), utcnow())
            summary["failed"] += 1
            summary["errors"][source.key] = str(result)[:300]
        else:
            apply_result(session, source, result, run)
            run.ok = True
            summary["ok"] += 1
            summary["new_jobs"] += run.jobs_new
            summary["closed_jobs"] += run.jobs_closed
        run.finished_at = utcnow()
        session.commit()
    return summary


# --------------------------------------------------------------------------
# housekeeping
# --------------------------------------------------------------------------

_WORD = re.compile(r"[a-z0-9]+")


def _title_key(title: str) -> str:
    words = _WORD.findall(title.lower())
    return " ".join(w for w in words if w not in {"i", "ii", "iii", "the", "a", "an", "and"})


def hide_aggregator_duplicates(session: Session) -> int:
    """Hide aggregator copies of jobs we already read straight from the employer."""
    direct = {}
    aggregated = []
    rows = session.execute(
        select(Job, Source.kind).join(Source, Job.source_id == Source.id).where(Job.status == "open")
    ).all()
    for job, kind in rows:
        if kind == "aggregator":
            aggregated.append(job)
        else:
            direct.setdefault(_title_key(job.title), []).append(job)
    hidden = 0
    for job in aggregated:
        for candidate in direct.get(_title_key(job.title), []):
            a, b = job.employer_slug, candidate.employer_slug
            if a == b or a.split("-")[0] == b.split("-")[0] or a in b or b in a:
                job.status, job.duplicate_of_id = "hidden", candidate.id
                hidden += 1
                break
    session.commit()
    return hidden


def close_past_deadline(session: Session) -> int:
    """Close jobs whose stated closing date has passed (manual posts rely on this)."""
    today = local_today()
    jobs = session.scalars(select(Job).where(Job.status == "open", Job.closes_on.is_not(None), Job.closes_on < today))
    count = 0
    for job in jobs:
        job.status, job.closed_at = "closed", utcnow()
        count += 1
    session.commit()
    return count


def run_once(session: Session, *, keys: list[str] | None = None, match: bool = True, alerts: bool = True) -> dict:
    from . import alerts as alerts_mod
    from . import matching

    worker = WorkerRun(started_at=utcnow())
    session.add(worker)
    session.commit()

    summary = run_sources(session, keys=keys)
    summary["duplicates_hidden"] = hide_aggregator_duplicates(session)
    summary["closed_past_deadline"] = close_past_deadline(session)
    if match:
        summary["matching"] = matching.score_all(session)
    if alerts:
        summary["alerts"] = alerts_mod.send_due(session)

    worker.finished_at = utcnow()
    worker.summary = summary
    session.commit()
    return summary


def preview_source(source: Source, limit: int = 15) -> dict:
    """Fetch a (possibly unsaved) source and report what it would add. Used by Admin."""
    result = _fetch(source)
    if isinstance(result, SourceError):
        return {"ok": False, "error": str(result), "found": 0, "kept": [], "dropped": 0}
    kept, dropped = [], 0
    for raw in result.jobs:
        values = normalize(raw, source)
        if values is None:
            dropped += 1
        else:
            kept.append(values)
    return {
        "ok": True,
        "found": len(result.jobs),
        "kept": kept[:limit],
        "kept_total": len(kept),
        "dropped": dropped,
        "page_fingerprint": result.page_fingerprint,
        "notes": result.notes,
    }


def recent_window(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)
