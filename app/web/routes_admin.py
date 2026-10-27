"""Admin: sources and their health, sign-up approvals, employer submissions, hand-posted jobs."""
from __future__ import annotations

from app.clock import local_today
import json
import re
import threading
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import mailer
from app.categories import CATEGORY_NAMES, EMPLOYMENT_TYPES
from app.config import get_settings
from app.db import SessionLocal, get_session
from app.models import (
    SOURCE_KINDS,
    AlertSubscription,
    AuditEvent,
    Job,
    Source,
    SourceRun,
    Submission,
    User,
    WorkerRun,
    utcnow,
)
from app.pipeline import normalize, preview_source, run_once
from app.region import COUNTIES, TOWNS
from app.sources import ADAPTERS, SELECTABLE, RawJob, detect
from app.sources.custom import SCRAPERS
from app.text import slugify

from .core import csrf_protect, current_user, flash, redirect, render, require_admin

router = APIRouter(prefix="/admin", dependencies=[Depends(current_user), Depends(require_admin), Depends(csrf_protect)])

_run_lock = threading.Lock()


def _audit(session: Session, actor: User, action: str, target: str | None = None, detail: str | None = None):
    session.add(AuditEvent(actor_id=actor.id, action=action, target=target, detail=detail))


# --------------------------------------------------------------------------
# overview
# --------------------------------------------------------------------------


@router.get("")
def overview(request: Request, session: Session = Depends(get_session)):
    sources = session.scalars(select(Source).order_by(Source.name)).all()
    health = {}
    for s in sources:
        health.setdefault(s.health, []).append(s)
    week = utcnow() - timedelta(days=7)
    stats = {
        "open_jobs": session.scalar(select(func.count()).select_from(Job).where(Job.status == "open")),
        "new_week": session.scalar(select(func.count()).select_from(Job).where(Job.first_seen_at >= week)),
        "users_active": session.scalar(select(func.count()).select_from(User).where(User.status == "active")),
        "users_pending": session.scalar(select(func.count()).select_from(User).where(User.status == "pending")),
        "subscribers": session.scalar(
            select(func.count()).select_from(AlertSubscription).where(
                AlertSubscription.confirmed_at.is_not(None), AlertSubscription.unsubscribed_at.is_(None)
            )
        ),
        "submissions": session.scalar(select(func.count()).select_from(Submission).where(Submission.status == "pending")),
    }
    last_run = session.scalar(select(WorkerRun).order_by(WorkerRun.id.desc()).limit(1))
    return render(request, "admin/overview.html", health=health, stats=stats, last_run=last_run,
                  running=_run_lock.locked())


def _background_run(keys: list[str] | None = None) -> None:
    if not _run_lock.acquire(blocking=False):
        return
    try:
        session = SessionLocal()
        try:
            run_once(session, keys=keys)
        finally:
            session.close()
    finally:
        _run_lock.release()


@router.post("/run")
def run_now(request: Request):
    if _run_lock.locked():
        flash(request, "A check is already running.")
    else:
        threading.Thread(target=_background_run, daemon=True).start()
        flash(request, "Checking every source now. Refresh in a minute or two to see results.")
    return redirect("/admin")


# --------------------------------------------------------------------------
# sources
# --------------------------------------------------------------------------


@router.get("/sources")
def sources(request: Request, session: Session = Depends(get_session)):
    rows = session.scalars(select(Source).where(Source.adapter != "manual").order_by(Source.name)).all()
    order = {"failing": 0, "warning": 1, "changed": 2, "new": 3, "ok": 4, "paused": 5}
    rows = sorted(rows, key=lambda s: (order.get(s.health, 9), s.name.lower()))
    return render(request, "admin/sources.html", rows=rows, adapters=ADAPTERS)


def _parse_config(adapter_key: str, form: dict) -> dict:
    """Read the adapter's settings fields from a submitted form."""
    cls = ADAPTERS[adapter_key]
    config: dict = {}
    for spec in cls.config_fields:
        raw = (form.get(f"cfg_{spec.name}") or "").strip()
        if not raw:
            continue
        if spec.kind == "list":
            config[spec.name] = [line.strip() for line in re.split(r"[\n,]", raw) if line.strip()]
        elif spec.kind == "int":
            try:
                config[spec.name] = int(raw)
            except ValueError:
                pass
        elif spec.kind == "districts":
            districts = []
            for line in raw.splitlines():
                parts = [p.strip() for p in line.split("|")]
                if parts and parts[0].isdigit():
                    districts.append({"id": int(parts[0]), "name": parts[1] if len(parts) > 1 else "",
                                      "portal": parts[2] if len(parts) > 2 else ""})
            config[spec.name] = districts
        else:
            config[spec.name] = raw
    extra = (form.get("cfg_extra_json") or "").strip()
    if extra:
        try:
            more = json.loads(extra)
            if isinstance(more, dict):
                config.update(more)
        except ValueError:
            pass
    return config


def _config_to_form(source: Source) -> dict:
    out: dict = {}
    cls = ADAPTERS.get(source.adapter)
    known = set()
    for spec in cls.config_fields if cls else ():
        known.add(spec.name)
        value = (source.config or {}).get(spec.name)
        if value is None:
            out[spec.name] = ""
        elif spec.kind == "list":
            out[spec.name] = "\n".join(value)
        elif spec.kind == "districts":
            out[spec.name] = "\n".join(f"{d.get('id')} | {d.get('name', '')} | {d.get('portal', '')}" for d in value)
        else:
            out[spec.name] = str(value)
    extra = {k: v for k, v in (source.config or {}).items() if k not in known}
    out["extra_json"] = json.dumps(extra) if extra else ""
    return out


def _source_from_form(form: dict, source: Source | None = None) -> Source:
    adapter = form.get("adapter") if form.get("adapter") in ADAPTERS else "jsonld"
    source = source or Source()
    source.name = (form.get("name") or "").strip()[:200] or "Unnamed source"
    source.kind = form.get("kind") if form.get("kind") in SOURCE_KINDS else "employer"
    source.adapter = adapter
    source.config = _parse_config(adapter, form)
    source.careers_url = (form.get("careers_url") or "").strip()[:1000] or None
    source.county = form.get("county") if form.get("county") in COUNTIES else None
    source.default_category = form.get("default_category") if form.get("default_category") in CATEGORY_NAMES else None
    source.notes = (form.get("notes") or "").strip() or None
    return source


def _form_context(source: Source, **extra) -> dict:
    return dict(
        source=source, cfg=_config_to_form(source), adapters=ADAPTERS, selectable=SELECTABLE,
        scrapers=SCRAPERS, kinds=SOURCE_KINDS, counties=list(COUNTIES), **extra,
    )


@router.get("/sources/new")
def source_new(request: Request, url: str = "", name: str = "", submission: int | None = None):
    source = Source(name=name, kind="employer", adapter="jsonld", config={}, careers_url=url or None)
    detection = None
    if url:
        detection = detect(url)
        source.adapter = detection.adapter
        source.config = detection.config
        if detection.adapter in ("neogov", "edjoin") or detection.config.get("scraper") in ("calcareers", "usajobs"):
            source.kind = "government"
    return render(request, "admin/source_form.html",
                  **_form_context(source, detection=detection, preview=None, submission_id=submission))


@router.post("/sources/new")
async def source_create(request: Request, session: Session = Depends(get_session)):
    form = dict(await request.form())
    source = _source_from_form(form)
    action = form.get("action")
    if action == "preview":
        preview = preview_source(source)
        return render(request, "admin/source_form.html",
                      **_form_context(source, detection=None, preview=preview, submission_id=form.get("submission_id")))
    base_key = slugify(source.name, 60).replace("-", "_") or "source"
    key, n = base_key, 2
    while session.scalar(select(Source.id).where(Source.key == key)):
        key, n = f"{base_key}_{n}", n + 1
    source.key = key
    source.active = True
    session.add(source)
    _audit(session, request.state.user, "source_added", key, json.dumps(source.config))
    session.commit()
    if form.get("submission_id"):
        _close_submission(session, int(form["submission_id"]), "approved", f"Added as source {key}")
    flash(request, f"Added {source.name}. It will be checked on the next run, or press Check now.")
    return redirect(f"/admin/sources/{source.id}")


@router.get("/sources/{source_id}")
def source_detail(source_id: int, request: Request, session: Session = Depends(get_session)):
    source = session.get(Source, source_id)
    if source is None:
        raise HTTPException(404)
    runs = session.scalars(
        select(SourceRun).where(SourceRun.source_id == source.id).order_by(SourceRun.id.desc()).limit(20)
    ).all()
    open_jobs = session.scalar(select(func.count()).select_from(Job).where(Job.source_id == source.id, Job.status == "open"))
    return render(request, "admin/source_form.html",
                  **_form_context(source, detection=None, preview=None, runs=runs, open_jobs=open_jobs))


@router.post("/sources/{source_id}")
async def source_update(source_id: int, request: Request, session: Session = Depends(get_session)):
    source = session.get(Source, source_id)
    if source is None:
        raise HTTPException(404)
    form = dict(await request.form())
    action = form.get("action")
    if action == "toggle":
        source.active = not source.active
        _audit(session, request.state.user, "source_paused" if not source.active else "source_resumed", source.key)
        session.commit()
        flash(request, f"{source.name} {'resumed' if source.active else 'paused'}.")
        return redirect(f"/admin/sources/{source.id}")
    if action == "check":
        if _run_lock.locked():
            flash(request, "A full check is running; try again in a minute.", "error")
            return redirect(f"/admin/sources/{source.id}")
        with _run_lock:
            summary = run_once(session, keys=[source.key], match=False, alerts=False)
        err = summary.get("errors", {}).get(source.key)
        flash(request, f"Check failed: {err}" if err else f"Checked. {summary['new_jobs']} new, {summary['closed_jobs']} closed.",
              "error" if err else "ok")
        return redirect(f"/admin/sources/{source.id}")
    if action == "ack_change":
        source.page_changed_at = None
        session.commit()
        flash(request, "Marked as reviewed.")
        return redirect(f"/admin/sources/{source.id}")

    if action == "preview":
        temp = _source_from_form(form, Source(id=source.id, key=source.key, active=source.active))
        preview = preview_source(temp)
        return render(request, "admin/source_form.html", **_form_context(temp, detection=None, preview=preview))
    _source_from_form(form, source)
    _audit(session, request.state.user, "source_updated", source.key, json.dumps(source.config))
    session.commit()
    flash(request, "Saved.")
    return redirect(f"/admin/sources/{source.id}")


# --------------------------------------------------------------------------
# users
# --------------------------------------------------------------------------


@router.get("/users")
def users(request: Request, status: str = "pending", session: Session = Depends(get_session)):
    query = select(User).order_by(User.created_at.desc())
    if status != "all":
        query = query.where(User.status == status)
    rows = session.scalars(query.limit(300)).all()
    counts = dict(session.execute(select(User.status, func.count()).group_by(User.status)).all())
    return render(request, "admin/users.html", rows=rows, status=status, counts=counts)


@router.post("/users/{user_id}")
def user_action(user_id: str, request: Request, action: str = Form(...), session: Session = Depends(get_session)):
    admin: User = request.state.user
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(404)
    settings = get_settings()
    if user.id == admin.id and action in ("deny", "suspend", "remove_admin"):
        flash(request, "You can't do that to your own account.", "error")
        return redirect("/admin/users?status=all")
    if action == "approve":
        user.status, user.approved_at = "active", utcnow()
        mailer.send(user.email, f"You're approved — {settings.site_name}", "approved",
                    user=user, link=f"{settings.public_base_url}/me/profile")
        flash(request, f"Approved {user.name}. We emailed them.")
    elif action == "deny":
        user.status = "denied"
        user.session_version += 1
        flash(request, f"Denied {user.name}.")
    elif action == "suspend":
        user.status = "suspended"
        user.session_version += 1
        flash(request, f"Suspended {user.name}.")
    elif action == "reactivate":
        user.status = "active"
        flash(request, f"Reactivated {user.name}.")
    elif action == "make_admin":
        user.is_admin = True
        flash(request, f"{user.name} is now an admin.")
    elif action == "remove_admin":
        user.is_admin = False
        flash(request, f"{user.name} is no longer an admin.")
    else:
        raise HTTPException(400, "Unknown action")
    _audit(session, admin, f"user_{action}", user.email)
    session.commit()
    return redirect(request.headers.get("referer") or "/admin/users")


# --------------------------------------------------------------------------
# submissions and hand-posted jobs
# --------------------------------------------------------------------------


@router.get("/submissions")
def submissions(request: Request, status: str = "pending", session: Session = Depends(get_session)):
    rows = session.scalars(
        select(Submission).where(Submission.status == status).order_by(Submission.created_at.desc()).limit(200)
    ).all()
    return render(request, "admin/submissions.html", rows=rows, status=status)


def _close_submission(session: Session, submission_id: int, status: str, note: str) -> Submission | None:
    sub = session.get(Submission, submission_id)
    if sub is None:
        return None
    sub.status, sub.review_note, sub.reviewed_at = status, note, utcnow()
    session.commit()
    return sub


@router.post("/submissions/{submission_id}")
def submission_action(submission_id: int, request: Request, action: str = Form(...), note: str = Form(""),
                      session: Session = Depends(get_session)):
    sub = session.get(Submission, submission_id)
    if sub is None:
        raise HTTPException(404)
    settings = get_settings()
    if action == "add_source":
        from urllib.parse import urlencode

        return redirect(f"/admin/sources/new?{urlencode({'url': sub.url or '', 'name': sub.employer})}&submission={sub.id}")
    if action == "post_job":
        return redirect(f"/admin/jobs/new?submission={sub.id}")
    if action == "reject":
        _close_submission(session, sub.id, "rejected", note.strip() or None)
        if note.strip():
            mailer.send(sub.contact_email, f"About your listing — {settings.site_name}", "submission_rejected",
                        sub=sub, note=note.strip())
        flash(request, "Rejected.")
    return redirect("/admin/submissions")


def _manual_source(session: Session) -> Source:
    source = session.scalar(select(Source).where(Source.key == "posted_directly"))
    if source is None:
        source = Source(key="posted_directly", name="Posted directly", kind="employer", adapter="manual",
                        config={}, active=False)
        session.add(source)
        session.flush()
    return source


@router.get("/jobs/new")
def job_new(request: Request, submission: int | None = None, session: Session = Depends(get_session)):
    sub = session.get(Submission, submission) if submission else None
    return render(request, "admin/job_form.html", sub=sub, towns=sorted(TOWNS), types=EMPLOYMENT_TYPES,
                  default_close=local_today() + timedelta(days=30))


@router.post("/jobs/new")
def job_create(
    request: Request,
    title: str = Form(...),
    employer: str = Form(...),
    town: str = Form(...),
    description: str = Form(...),
    apply_url: str = Form(...),
    pay: str = Form(""),
    employment_type: str = Form(""),
    closes_on: str = Form(...),
    submission_id: str = Form(""),
    session: Session = Depends(get_session),
):
    source = _manual_source(session)
    try:
        closes = date.fromisoformat(closes_on)
    except ValueError:
        closes = local_today() + timedelta(days=30)
    if not apply_url.startswith(("http://", "https://", "mailto:")):
        apply_url = f"mailto:{apply_url}" if "@" in apply_url else f"https://{apply_url}"
    raw = RawJob(
        external_id=f"manual-{utcnow().timestamp():.0f}-{slugify(title, 30)}",
        title=title.strip(), apply_url=apply_url.strip(), employer=employer.strip(),
        location=f"{town}, CA" if town in TOWNS else town, description=description.strip(),
        employment_type=employment_type or None, salary_text=pay.strip() or None,
        posted_on=local_today(), closes_on=closes,
    )
    values = normalize(raw, source)
    if values is None:
        flash(request, "That town isn't in the area the board covers, so the job wasn't added.", "error")
        return redirect("/admin/jobs/new")
    new_job = Job(source_id=source.id, status="open", listed_on=local_today(), **values)
    session.add(new_job)
    session.flush()
    _audit(session, request.state.user, "job_posted", str(new_job.id), new_job.title)
    session.commit()
    if submission_id:
        sub = _close_submission(session, int(submission_id), "approved", f"Posted as job {new_job.id}")
        if sub:
            settings = get_settings()
            mailer.send(sub.contact_email, f"Your job is live — {settings.site_name}", "submission_live",
                        sub=sub, link=f"{settings.public_base_url}/jobs/{new_job.id}/{new_job.slug}")
    flash(request, "Posted.")
    return redirect(f"/jobs/{new_job.id}/{new_job.slug}")


@router.get("/jobs")
def jobs(request: Request, q: str = "", session: Session = Depends(get_session)):
    query = select(Job).order_by(Job.first_seen_at.desc()).limit(100)
    if q:
        query = query.where(Job.title.ilike(f"%{q}%") | Job.employer.ilike(f"%{q}%"))
    return render(request, "admin/jobs.html", rows=session.scalars(query).all(), q=q)


@router.post("/jobs/{job_id}")
def job_action(job_id: int, request: Request, action: str = Form(...), session: Session = Depends(get_session)):
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(404)
    if action == "hide":
        job.status = "hidden"
        flash(request, "Hidden from the board.")
    elif action == "unhide":
        job.status, job.duplicate_of_id = "open", None
        flash(request, "Back on the board.")
    elif action == "close":
        job.status, job.closed_at = "closed", utcnow()
        flash(request, "Closed.")
    _audit(session, request.state.user, f"job_{action}", str(job.id), job.title)
    session.commit()
    return redirect(request.headers.get("referer") or "/admin/jobs")


@router.get("/activity")
def activity(request: Request, session: Session = Depends(get_session)):
    runs = session.scalars(select(WorkerRun).order_by(WorkerRun.id.desc()).limit(30)).all()
    events = session.scalars(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(100)).all()
    return render(request, "admin/activity.html", runs=runs, events=events)
