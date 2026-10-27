"""Pages anyone can see without an account."""
from __future__ import annotations

import json
import secrets
from datetime import date, timedelta
from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import mailer
from app.categories import CATEGORY_NAMES
from app.config import get_settings
from app.db import get_session
from app.models import AlertSubscription, Job, Match, Source, Submission, TrackedJob, utcnow
from app.region import COUNTIES, PRIMARY_COUNTIES, TOWN_SLUGS, TOWNS
from app.text import slugify, truncate

from .core import EMAIL_RE, csrf_protect, current_user, flash, notify_admins, rate_limit, redirect, render
from .search import COUNTY_SLUGS, POSTED_CHOICES, Filters, facet_counts, search

router = APIRouter(dependencies=[Depends(current_user), Depends(csrf_protect)])


def _public_cache(response: Response, request: Request, seconds: int = 300) -> None:
    if getattr(request.state, "user", None) is None:
        response.headers["Cache-Control"] = f"public, max-age={seconds}"


def _stats(session: Session) -> dict:
    open_jobs = session.scalar(select(func.count()).select_from(Job).where(Job.status == "open", Job.is_remote.is_(False))) or 0
    employers = session.scalar(
        select(func.count(func.distinct(Job.employer_slug))).where(Job.status == "open", Job.is_remote.is_(False))
    ) or 0
    new_week = session.scalar(
        select(func.count()).select_from(Job).where(
            Job.status == "open", Job.is_remote.is_(False), Job.first_seen_at >= utcnow() - timedelta(days=7)
        )
    ) or 0
    checked = session.scalar(select(func.max(Source.last_success_at)))
    return {"open_jobs": open_jobs, "employers": employers, "new_week": new_week, "checked": checked}


# --------------------------------------------------------------------------
# home and browsing
# --------------------------------------------------------------------------


@router.get("/")
def home(request: Request, session: Session = Depends(get_session)):
    latest, _ = search(session, Filters(), page_size=12)
    by_county = dict(
        session.execute(
            select(Job.county, func.count()).where(Job.status == "open").group_by(Job.county)
        ).all()
    )
    by_category = dict(
        session.execute(
            select(Job.category, func.count()).where(Job.status == "open", Job.is_remote.is_(False)).group_by(Job.category)
        ).all()
    )
    response = render(
        request, "public/home.html",
        latest=latest, stats=_stats(session), by_county=by_county, by_category=by_category,
        county_slugs={v: k for k, v in COUNTY_SLUGS.items()},
    )
    _public_cache(response, request, 120)
    return response


@router.get("/jobs")
def jobs(request: Request, session: Session = Depends(get_session)):
    filters = Filters.from_query(request.query_params)
    results, total = search(session, filters)
    response = render(
        request, "public/jobs.html",
        filters=filters, jobs=results, total=total, facets=facet_counts(session, filters),
        posted_choices=POSTED_CHOICES, county_slugs={v: k for k, v in COUNTY_SLUGS.items()},
        pages=(total + 24) // 25, heading=_heading(filters), saved_ids=_saved_ids(request, session),
    )
    _public_cache(response, request)
    return response


def _heading(filters: Filters) -> str:
    where = filters.town_name or filters.county_name
    what = filters.category or "Jobs"
    return f"{what} in {where}" if where else ("Local jobs" if what == "Jobs" else what)


def _saved_ids(request: Request, session: Session) -> set[int]:
    user = request.state.user
    if user is None:
        return set()
    return set(session.scalars(select(TrackedJob.job_id).where(TrackedJob.user_id == user.id)))


@router.get("/in/{town_slug}")
def town_page(town_slug: str, request: Request):
    if town_slug in TOWN_SLUGS:
        return redirect(f"/jobs?town={town_slug}")
    if town_slug in COUNTY_SLUGS:
        return redirect(f"/jobs?county={town_slug}")
    raise HTTPException(404)


@router.get("/jobs/{job_id}")
@router.get("/jobs/{job_id}/{slug}")
def job_detail(job_id: int, request: Request, slug: str | None = None, session: Session = Depends(get_session)):
    job = session.get(Job, job_id)
    if job is None or job.status == "hidden":
        if job is not None and job.duplicate_of_id:
            return redirect(f"/jobs/{job.duplicate_of_id}")
        raise HTTPException(404)
    if slug != job.slug:
        return redirect(f"/jobs/{job.id}/{job.slug}")

    user = request.state.user
    match = tracked = None
    if user is not None:
        match = session.scalar(select(Match).where(Match.user_id == user.id, Match.job_id == job.id))
        tracked = session.scalar(select(TrackedJob).where(TrackedJob.user_id == user.id, TrackedJob.job_id == job.id))

    more_here = session.scalars(
        select(Job).where(Job.status == "open", Job.employer_slug == job.employer_slug, Job.id != job.id)
        .order_by(Job.listed_on.desc()).limit(5)
    ).all()
    response = render(
        request, "public/job.html",
        job=job, match=match, tracked=tracked, more_here=more_here,
        json_ld=_job_json_ld(job) if job.is_open else None,
        canonical=f"{get_settings().public_base_url}/jobs/{job.id}/{job.slug}",
        description_meta=truncate(f"{job.title} at {job.employer} in {job.town or job.county or 'the Mother Lode'}. {job.description or ''}", 155),
    )
    _public_cache(response, request)
    return response


def _job_json_ld(job: Job) -> str:
    """schema.org JobPosting so search engines can list the job."""
    data: dict = {
        "@context": "https://schema.org/",
        "@type": "JobPosting",
        "title": job.title,
        "description": (job.description or job.title).replace("\n", "<br>"),
        "datePosted": job.listed_on.isoformat(),
        "hiringOrganization": {"@type": "Organization", "name": job.employer},
        "identifier": {"@type": "PropertyValue", "name": job.employer, "value": job.external_id},
        "directApply": False,
    }
    if job.closes_on:
        data["validThrough"] = f"{job.closes_on.isoformat()}T23:59:00-08:00"
    else:
        data["validThrough"] = f"{(job.listed_on + timedelta(days=60)).isoformat()}T23:59:00-08:00"
    if job.is_remote:
        data["jobLocationType"] = "TELECOMMUTE"
        data["applicantLocationRequirements"] = {"@type": "State", "name": "California"}
    else:
        data["jobLocation"] = {
            "@type": "Place",
            "address": {
                "@type": "PostalAddress",
                "addressLocality": job.town or (job.county or ""),
                "addressRegion": "CA",
                "addressCountry": "US",
            },
        }
    type_map = {"Full-time": "FULL_TIME", "Part-time": "PART_TIME", "Seasonal": "TEMPORARY",
                "Temporary": "TEMPORARY", "Per diem": "PER_DIEM", "Internship": "INTERN"}
    if job.employment_type in type_map:
        data["employmentType"] = type_map[job.employment_type]
    if job.salary_min and job.salary_unit:
        value = {"@type": "QuantitativeValue", "minValue": job.salary_min, "unitText": job.salary_unit.upper()}
        if job.salary_max and job.salary_max != job.salary_min:
            value["maxValue"] = job.salary_max
        data["baseSalary"] = {"@type": "MonetaryAmount", "currency": "USD", "value": value}
    return json.dumps(data).replace("</", "<\\/")


@router.get("/employers")
def employers(request: Request, session: Session = Depends(get_session)):
    rows = session.execute(
        select(Job.employer, Job.employer_slug, func.count(), func.max(Job.county))
        .where(Job.status == "open", Job.is_remote.is_(False))
        .group_by(Job.employer, Job.employer_slug)
        .order_by(Job.employer)
    ).all()
    sources = session.scalars(
        select(Source).where(Source.active.is_(True), Source.kind.in_(("employer", "government")))
        .order_by(Source.name)
    ).all()
    response = render(request, "public/employers.html", rows=rows, sources=sources)
    _public_cache(response, request)
    return response


@router.get("/employers/{employer_slug}")
def employer_page(employer_slug: str, request: Request):
    return redirect(f"/jobs?employer={employer_slug}")


# --------------------------------------------------------------------------
# email-only alerts
# --------------------------------------------------------------------------


@router.get("/alerts")
def alerts_form(request: Request):
    filters = Filters.from_query(request.query_params)
    return render(request, "public/alerts.html", filters=filters, county_slugs={v: k for k, v in COUNTY_SLUGS.items()})


@router.post("/alerts", dependencies=[Depends(rate_limit("alerts", 6, 600))])
def alerts_subscribe(
    request: Request,
    email: str = Form(...),
    keywords: str = Form(""),
    counties: list[str] = Form([]),
    categories: list[str] = Form([]),
    include_remote: str = Form(""),
    frequency: str = Form("weekly"),
    website: str = Form(""),  # honeypot
    session: Session = Depends(get_session),
):
    email = email.strip().lower()
    if website:
        return redirect("/alerts/check-email")
    if not EMAIL_RE.match(email):
        flash(request, "That email address doesn't look right.", "error")
        return redirect("/alerts")
    sub = AlertSubscription(
        email=email,
        keywords=keywords.strip()[:200] or None,
        counties=[c for c in counties if c in COUNTIES],
        categories=[c for c in categories if c in CATEGORY_NAMES],
        include_remote=bool(include_remote),
        frequency="daily" if frequency == "daily" else "weekly",
        token=secrets.token_urlsafe(32),
    )
    session.add(sub)
    session.commit()
    mailer.send(email, f"Confirm your job alert — {get_settings().site_name}", "confirm_alert", sub=sub)
    return redirect("/alerts/check-email")


@router.get("/alerts/check-email")
def alerts_check_email(request: Request):
    return render(request, "public/message.html", title="Check your email",
                  body="We sent you a link to confirm your job alert. It only starts once you click it.")


@router.get("/alerts/confirm/{token}")
def alerts_confirm(token: str, request: Request, session: Session = Depends(get_session)):
    sub = session.scalar(select(AlertSubscription).where(AlertSubscription.token == token))
    if sub is None:
        raise HTTPException(404)
    if sub.confirmed_at is None:
        sub.confirmed_at = utcnow()
        sub.unsubscribed_at = None
        session.commit()
    return render(request, "public/message.html", title="Your job alert is on",
                  body=f"We'll email {sub.email} {'each morning' if sub.frequency == 'daily' else 'once a week'} "
                       "when new jobs match. Every email has a one-click unsubscribe link.")


@router.get("/alerts/unsubscribe/{token}")
def alerts_unsubscribe_page(token: str, request: Request, session: Session = Depends(get_session)):
    sub = session.scalar(select(AlertSubscription).where(AlertSubscription.token == token))
    if sub is None:
        raise HTTPException(404)
    return render(request, "public/unsubscribe.html", sub=sub)


@router.post("/alerts/unsubscribe/{token}")
def alerts_unsubscribe(token: str, request: Request, session: Session = Depends(get_session)):
    sub = session.scalar(select(AlertSubscription).where(AlertSubscription.token == token))
    if sub is None:
        raise HTTPException(404)
    sub.unsubscribed_at = utcnow()
    session.commit()
    return render(request, "public/message.html", title="You're unsubscribed",
                  body="You won't get this job alert anymore.")


# --------------------------------------------------------------------------
# employers telling us about jobs
# --------------------------------------------------------------------------


@router.get("/for-employers")
def employer_form(request: Request):
    return render(request, "public/for_employers.html", towns=sorted(TOWNS))


@router.post("/for-employers", dependencies=[Depends(rate_limit("submit", 5, 3600))])
def employer_submit(
    request: Request,
    kind: str = Form("careers_page"),
    employer: str = Form(...),
    contact_name: str = Form(""),
    contact_email: str = Form(...),
    url: str = Form(""),
    job_title: str = Form(""),
    town: str = Form(""),
    description: str = Form(""),
    pay: str = Form(""),
    closes_on: str = Form(""),
    website: str = Form(""),  # honeypot
    session: Session = Depends(get_session),
):
    if website:
        return redirect("/for-employers/thanks")
    errors = []
    if not employer.strip():
        errors.append("Add your business name.")
    if not EMAIL_RE.match(contact_email.strip()):
        errors.append("Add an email address we can reach you at.")
    if kind == "careers_page" and not url.strip().startswith(("http://", "https://")):
        errors.append("Add the full link to your careers page, starting with https://")
    if kind == "single_job" and (not job_title.strip() or not description.strip()):
        errors.append("Add the job title and a short description.")
    if errors:
        for e in errors:
            flash(request, e, "error")
        return redirect(f"/for-employers?kind={kind}")
    try:
        closes = date.fromisoformat(closes_on) if closes_on else None
    except ValueError:
        closes = None
    session.add(
        Submission(
            kind="single_job" if kind == "single_job" else "careers_page",
            employer=employer.strip()[:200], contact_name=contact_name.strip()[:120] or None,
            contact_email=contact_email.strip().lower()[:320], url=url.strip()[:2000] or None,
            job_title=job_title.strip()[:300] or None, town=town if town in TOWNS else None,
            description=description.strip()[:8000] or None, pay=pay.strip()[:200] or None, closes_on=closes,
        )
    )
    session.commit()
    notify_admins(session, f"New employer submission: {employer.strip()}")
    return redirect("/for-employers/thanks")


@router.get("/for-employers/thanks")
def employer_thanks(request: Request):
    return render(request, "public/message.html", title="Thanks — we got it",
                  body="We'll review it within a couple of days and email you once your jobs are on the board.")


# --------------------------------------------------------------------------
# static-ish pages and crawlers
# --------------------------------------------------------------------------


@router.get("/about")
def about(request: Request, session: Session = Depends(get_session)):
    sources = session.scalars(select(Source).where(Source.active.is_(True), Source.adapter != "manual").order_by(Source.name)).all()
    return render(request, "public/about.html", sources=sources, stats=_stats(session))


@router.get("/privacy")
def privacy(request: Request):
    return render(request, "public/privacy.html")


@router.get("/healthz")
def healthz():
    return PlainTextResponse("ok")


@router.get("/robots.txt")
def robots():
    base = get_settings().public_base_url
    return PlainTextResponse(
        "User-agent: *\nDisallow: /admin\nDisallow: /me\nDisallow: /login\nDisallow: /signup\n"
        "Disallow: /alerts/\n\n"
        f"Sitemap: {base}/sitemap.xml\n"
    )


@router.get("/sitemap.xml")
def sitemap(session: Session = Depends(get_session)):
    base = get_settings().public_base_url
    urls = [(f"{base}/", None), (f"{base}/jobs", None), (f"{base}/employers", None), (f"{base}/for-employers", None)]
    for slug in COUNTY_SLUGS:
        if COUNTY_SLUGS[slug] in PRIMARY_COUNTIES:
            urls.append((f"{base}/jobs?county={slug}", None))
    towns_with_jobs = set(session.scalars(select(Job.town).where(Job.status == "open", Job.town.is_not(None)).distinct()))
    for town in sorted(towns_with_jobs):
        urls.append((f"{base}/jobs?town={slugify(town)}", None))
    for job_id, slug, seen in session.execute(
        select(Job.id, Job.slug, Job.last_seen_at).where(Job.status == "open").order_by(Job.id.desc()).limit(45000)
    ):
        urls.append((f"{base}/jobs/{job_id}/{slug}", seen))
    body = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for loc, lastmod in urls:
        body.append(f"<url><loc>{escape(loc)}</loc>" + (f"<lastmod>{lastmod.date().isoformat()}</lastmod>" if lastmod else "") + "</url>")
    body.append("</urlset>")
    return Response("\n".join(body), media_type="application/xml", headers={"Cache-Control": "public, max-age=900"})
