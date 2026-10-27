"""Sign-up, sign-in, and everything a job seeker does with an account."""
from __future__ import annotations

from app.clock import local_today
from datetime import date

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import mailer, matching
from app.categories import CATEGORY_NAMES, EMPLOYMENT_TYPES
from app.config import get_settings
from app.db import get_session
from app.models import AuditEvent, Job, Match, Profile, TrackedJob, TRACK_STATUSES, User, utcnow
from app.region import COUNTIES, TOWNS
from app.resumes import ResumeError, extract_text
from app.security import hash_password, issue_token, password_problem, use_token, verify_password

from .core import EMAIL_RE, csrf_protect, current_user, flash, notify_admins, rate_limit, redirect, render, require_active, require_user

router = APIRouter(dependencies=[Depends(current_user), Depends(csrf_protect)])


def _audit(session: Session, actor: User | None, action: str, target: str | None = None, detail: str | None = None):
    session.add(AuditEvent(actor_id=actor.id if actor else None, action=action, target=target, detail=detail))


def _sign_in(request: Request, user: User) -> None:
    request.session.clear()
    request.session["uid"] = user.id
    request.session["sv"] = user.session_version


def _safe_next(value: str | None) -> str:
    return value if value and value.startswith("/") and not value.startswith("//") else "/me"


def _lines(value: str, limit: int = 30, length: int = 80) -> list[str]:
    items = []
    for raw in value.replace(",", "\n").splitlines():
        item = raw.strip()[:length]
        if item and item.lower() not in {i.lower() for i in items}:
            items.append(item)
    return items[:limit]


# --------------------------------------------------------------------------
# sign up / verify / sign in
# --------------------------------------------------------------------------


@router.get("/signup")
def signup_form(request: Request):
    if request.state.user:
        return redirect("/me")
    return render(request, "account/signup.html", mode=get_settings().signup_mode)


@router.post("/signup", dependencies=[Depends(rate_limit("signup", 5, 3600))])
def signup(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    note: str = Form(""),
    website: str = Form(""),  # honeypot
    session: Session = Depends(get_session),
):
    settings = get_settings()
    if settings.signup_mode == "closed":
        raise HTTPException(404)
    email = email.strip().lower()
    name = name.strip()[:120]
    form = {"name": name, "email": email, "note": note}
    if website:
        return redirect("/signup/check-email")
    error = None
    if not name:
        error = "Add your name."
    elif not EMAIL_RE.match(email):
        error = "That email address doesn't look right."
    else:
        error = password_problem(password, email)
    if error:
        return render(request, "account/signup.html", status_code=400, error=error, form=form, mode=settings.signup_mode)

    existing = session.scalar(select(User).where(User.email == email))
    if existing is not None:
        # Don't reveal whether an address is registered; nudge them by email instead.
        mailer.send(email, f"You already have an account — {settings.site_name}", "already_registered", user=existing)
        return redirect("/signup/check-email")

    user = User(email=email, name=name, password_hash=hash_password(password), status="unverified",
                signup_note=note.strip()[:1000] or None)
    session.add(user)
    session.flush()
    session.add(Profile(user_id=user.id, counties=[], towns=[], categories=[], target_roles=[], skills=[]))
    raw = issue_token(session, user, "verify_email", hours=48)
    _audit(session, user, "signup", user.email)
    session.commit()
    mailer.send(email, f"Confirm your email — {settings.site_name}", "verify_email",
                user=user, link=f"{settings.public_base_url}/verify/{raw}")
    return redirect("/signup/check-email")


@router.get("/signup/check-email")
def signup_check(request: Request):
    return render(request, "public/message.html", title="Check your email",
                  body="We sent you a link to confirm your email address. It works for 48 hours.")


@router.get("/verify/{token}")
def verify(token: str, request: Request, session: Session = Depends(get_session)):
    settings = get_settings()
    user = use_token(session, token, "verify_email")
    if user is None:
        return render(request, "public/message.html", status_code=400, title="That link has expired",
                      body="Sign in and we'll offer to send a fresh one.")
    user.verified_at = user.verified_at or utcnow()
    if user.status == "unverified":
        if user.email in settings.admin_emails:
            user.status, user.is_admin, user.approved_at = "active", True, utcnow()
        elif settings.signup_mode == "open":
            user.status, user.approved_at = "active", utcnow()
        else:
            user.status = "pending"
            notify_admins(session, f"New sign-up waiting for approval: {user.name} ({user.email})")
    _audit(session, user, "email_verified", user.email)
    session.commit()
    _sign_in(request, user)
    if user.is_active:
        flash(request, "Email confirmed. Tell us what you're looking for and we'll start matching jobs.")
        return redirect("/me/profile")
    return redirect("/me")


@router.post("/verify/resend")
def resend_verification(request: Request, user: User = Depends(require_user), session: Session = Depends(get_session)):
    if user.status != "unverified":
        return redirect("/me")
    settings = get_settings()
    raw = issue_token(session, user, "verify_email", hours=48)
    session.commit()
    mailer.send(user.email, f"Confirm your email — {settings.site_name}", "verify_email",
                user=user, link=f"{settings.public_base_url}/verify/{raw}")
    flash(request, "We sent a new confirmation link.")
    return redirect("/me")


@router.get("/login")
def login_form(request: Request, next: str = ""):
    if request.state.user:
        return redirect(_safe_next(next))
    return render(request, "account/login.html", next=next)


@router.post("/login", dependencies=[Depends(rate_limit("login", 10, 600))])
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form(""),
    session: Session = Depends(get_session),
):
    user = session.scalar(select(User).where(User.email == email.strip().lower()))
    if user is None or not verify_password(password, user.password_hash):
        return render(request, "account/login.html", status_code=400, next=next, email=email,
                      error="That email and password don't match.")
    if user.status in ("denied", "suspended"):
        return render(request, "account/login.html", status_code=403, next=next, email=email,
                      error="This account can't sign in. Reply to any email from us if you think that's a mistake.")
    user.last_login_at = utcnow()
    _audit(session, user, "login")
    session.commit()
    _sign_in(request, user)
    return redirect(_safe_next(next))


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return redirect("/")


@router.get("/forgot")
def forgot_form(request: Request):
    return render(request, "account/forgot.html")


@router.post("/forgot", dependencies=[Depends(rate_limit("forgot", 5, 3600))])
def forgot(request: Request, email: str = Form(...), session: Session = Depends(get_session)):
    settings = get_settings()
    user = session.scalar(select(User).where(User.email == email.strip().lower()))
    if user is not None and user.status not in ("denied", "suspended"):
        raw = issue_token(session, user, "reset_password", hours=2)
        session.commit()
        mailer.send(user.email, f"Reset your password — {settings.site_name}", "reset_password",
                    user=user, link=f"{settings.public_base_url}/reset/{raw}")
    return render(request, "public/message.html", title="Check your email",
                  body="If that address has an account, we sent a link to reset the password. It works for 2 hours.")


@router.get("/reset/{token}")
def reset_form(token: str, request: Request, session: Session = Depends(get_session)):
    if use_token(session, token, "reset_password", consume=False) is None:
        return render(request, "public/message.html", status_code=400, title="That link has expired",
                      body="Reset links work for 2 hours. Ask for a new one from the sign-in page.")
    return render(request, "account/reset.html", token=token)


@router.post("/reset/{token}")
def reset(token: str, request: Request, password: str = Form(...), session: Session = Depends(get_session)):
    user = use_token(session, token, "reset_password", consume=False)
    if user is None:
        return redirect("/forgot")
    problem = password_problem(password, user.email)
    if problem:
        return render(request, "account/reset.html", status_code=400, token=token, error=problem)
    use_token(session, token, "reset_password")
    user.password_hash = hash_password(password)
    user.session_version += 1  # sign out everywhere else
    if user.status == "unverified":
        # They proved they control the inbox.
        user.verified_at = utcnow()
        settings = get_settings()
        user.status = "active" if settings.signup_mode == "open" else "pending"
    _audit(session, user, "password_reset")
    session.commit()
    _sign_in(request, user)
    flash(request, "Password changed.")
    return redirect("/me")


# --------------------------------------------------------------------------
# dashboard: matches
# --------------------------------------------------------------------------


@router.get("/me")
def dashboard(request: Request, user: User = Depends(require_user), session: Session = Depends(get_session),
              show: str = "good"):
    if not user.is_active:
        return render(request, "account/pending.html")
    profile = user.profile
    matches = []
    if profile and profile.is_complete:
        query = (
            select(Match).join(Job, Job.id == Match.job_id)
            .where(Match.user_id == user.id, Job.status == "open", Match.dismissed.is_(False))
        )
        if show == "good":
            query = query.where(Match.score >= 50)
        matches = session.scalars(query.order_by(Match.score.desc(), Job.listed_on.desc()).limit(60)).all()
    tracked_ids = set(session.scalars(select(TrackedJob.job_id).where(TrackedJob.user_id == user.id)))
    return render(
        request, "account/dashboard.html",
        profile=profile, matches=matches, show=show, tracked_ids=tracked_ids,
        ai_left=matching.ai_budget_left(session, user.id), ai_enabled=get_settings().ai_enabled,
    )


@router.post("/me/rescore")
def rescore(request: Request, user: User = Depends(require_active), session: Session = Depends(get_session)):
    result = matching.score_user(session, user, ai=False)
    flash(request, f"Checked {result['scored']} jobs against your profile." if result["scored"] else "Your matches are up to date.")
    return redirect("/me")


@router.post("/matches/{match_id}/dismiss")
def dismiss(match_id: int, request: Request, user: User = Depends(require_active), session: Session = Depends(get_session)):
    match = session.get(Match, match_id)
    if match is None or match.user_id != user.id:
        raise HTTPException(404)
    match.dismissed = True
    session.commit()
    return redirect(request.headers.get("referer") or "/me")


@router.post("/jobs/{job_id}/review")
def ai_review(job_id: int, request: Request, user: User = Depends(require_active), session: Session = Depends(get_session)):
    job = session.get(Job, job_id)
    profile = user.profile
    if job is None or profile is None:
        raise HTTPException(404)
    match = session.scalar(select(Match).where(Match.user_id == user.id, Match.job_id == job.id))
    if match is None:
        score, strengths, summary = matching.quick_score(job, profile)
        match = Match(user_id=user.id, job_id=job.id, job=job, score=score, strengths=strengths, gaps=[],
                      summary=summary, method="quick", profile_version=profile.version)
        session.add(match)
        session.flush()
    if matching.review_with_ai(session, match, profile):
        session.commit()
        flash(request, "Here's a closer look at how you fit this job.")
    else:
        session.commit()
        flash(request, "Detailed reviews are used up for today. Your quick score is shown instead.", "error")
    return redirect(f"/jobs/{job.id}/{job.slug}#fit")


# --------------------------------------------------------------------------
# profile
# --------------------------------------------------------------------------


@router.get("/me/profile")
def profile_form(request: Request, user: User = Depends(require_active)):
    return render(request, "account/profile.html", profile=user.profile, towns=sorted(TOWNS), counties=list(COUNTIES))


@router.post("/me/profile")
def profile_save(
    request: Request,
    target_roles: str = Form(""),
    skills: str = Form(""),
    experience_summary: str = Form(""),
    counties: list[str] = Form([]),
    towns: list[str] = Form([]),
    categories: list[str] = Form([]),
    employment_types: list[str] = Form([]),
    include_remote: str = Form(""),
    min_hourly_pay: str = Form(""),
    must_avoid: str = Form(""),
    alert_frequency: str = Form("daily"),
    alert_min_score: int = Form(60),
    user: User = Depends(require_active),
    session: Session = Depends(get_session),
):
    profile = user.profile or Profile(user_id=user.id)
    if profile not in session:
        session.add(profile)
    before = (profile.target_roles, profile.skills, profile.experience_summary, profile.counties, profile.towns,
              profile.categories, profile.employment_types, profile.include_remote, profile.min_hourly_pay, profile.must_avoid)
    profile.target_roles = _lines(target_roles, 10)
    profile.skills = _lines(skills, 40)
    profile.experience_summary = experience_summary.strip()[:3000] or None
    profile.counties = [c for c in counties if c in COUNTIES]
    profile.towns = [t for t in towns if t in TOWNS]
    profile.categories = [c for c in categories if c in CATEGORY_NAMES]
    profile.employment_types = [t for t in employment_types if t in EMPLOYMENT_TYPES]
    profile.include_remote = bool(include_remote)
    try:
        profile.min_hourly_pay = round(float(min_hourly_pay), 2) if min_hourly_pay.strip() else None
    except ValueError:
        profile.min_hourly_pay = None
    profile.must_avoid = _lines(must_avoid, 15, 40)
    profile.alert_frequency = alert_frequency if alert_frequency in ("off", "daily", "weekly") else "daily"
    profile.alert_min_score = max(0, min(100, alert_min_score))
    after = (profile.target_roles, profile.skills, profile.experience_summary, profile.counties, profile.towns,
             profile.categories, profile.employment_types, profile.include_remote, profile.min_hourly_pay, profile.must_avoid)
    if after != before:
        profile.version = (profile.version or 1) + 1
    profile.updated_at = utcnow()
    session.commit()
    if not profile.is_complete:
        flash(request, "Saved. Add at least one kind of job (or a resume) and one place to start getting matches.", "error")
        return redirect("/me/profile")
    result = matching.score_user(session, user, ai=False)
    flash(request, f"Saved. We checked {result['scored']} open jobs against your profile.")
    return redirect("/me")


@router.post("/me/resume")
async def resume_upload(
    request: Request,
    file: UploadFile = File(...),
    user: User = Depends(require_active),
    session: Session = Depends(get_session),
):
    content = await file.read()
    try:
        text = extract_text(file.filename or "", content)
    except ResumeError as exc:
        flash(request, str(exc), "error")
        return redirect("/me/profile#resume")
    profile = user.profile
    profile.resume_text = text
    profile.resume_filename = (file.filename or "resume")[:300]
    profile.resume_uploaded_at = utcnow()
    profile.version = (profile.version or 1) + 1
    session.commit()
    flash(request, "Resume read. We use its text to match jobs; the file itself isn't stored.")
    return redirect("/me/profile#resume")


@router.post("/me/resume/delete")
def resume_delete(request: Request, user: User = Depends(require_active), session: Session = Depends(get_session)):
    profile = user.profile
    profile.resume_text = profile.resume_filename = profile.resume_uploaded_at = None
    profile.version = (profile.version or 1) + 1
    session.commit()
    flash(request, "Resume removed.")
    return redirect("/me/profile#resume")


# --------------------------------------------------------------------------
# saved & applied jobs
# --------------------------------------------------------------------------


@router.post("/jobs/{job_id}/track")
def track(job_id: int, request: Request, status: str = Form("saved"), user: User = Depends(require_active),
          session: Session = Depends(get_session)):
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(404)
    tracked = session.scalar(select(TrackedJob).where(TrackedJob.user_id == user.id, TrackedJob.job_id == job.id))
    if status == "remove":
        if tracked:
            session.delete(tracked)
        session.commit()
        flash(request, "Removed from your list.")
        return redirect(request.headers.get("referer") or f"/jobs/{job.id}/{job.slug}")
    if status not in TRACK_STATUSES:
        status = "saved"
    if tracked is None:
        tracked = TrackedJob(user_id=user.id, job_id=job.id)
        session.add(tracked)
    tracked.status = status
    if status == "applied" and not tracked.applied_on:
        tracked.applied_on = local_today()
    tracked.updated_at = utcnow()
    session.commit()
    flash(request, "Marked as applied. Good luck!" if status == "applied" else "Saved to your list.")
    return redirect(request.headers.get("referer") or f"/jobs/{job.id}/{job.slug}")


@router.get("/me/jobs")
def tracker(request: Request, user: User = Depends(require_active), session: Session = Depends(get_session)):
    rows = session.scalars(
        select(TrackedJob).where(TrackedJob.user_id == user.id).order_by(TrackedJob.updated_at.desc())
    ).all()
    return render(request, "account/tracker.html", rows=rows, statuses=TRACK_STATUSES)


@router.post("/me/jobs/{tracked_id}")
def tracker_update(
    tracked_id: int,
    request: Request,
    status: str = Form(...),
    applied_on: str = Form(""),
    follow_up_on: str = Form(""),
    notes: str = Form(""),
    user: User = Depends(require_active),
    session: Session = Depends(get_session),
):
    tracked = session.get(TrackedJob, tracked_id)
    if tracked is None or tracked.user_id != user.id:
        raise HTTPException(404)

    def parse(value: str):
        try:
            return date.fromisoformat(value) if value else None
        except ValueError:
            return None

    tracked.status = status if status in TRACK_STATUSES else tracked.status
    tracked.applied_on = parse(applied_on)
    tracked.follow_up_on = parse(follow_up_on)
    tracked.notes = notes.strip()[:4000] or None
    tracked.updated_at = utcnow()
    session.commit()
    flash(request, "Updated.")
    return redirect("/me/jobs")


# --------------------------------------------------------------------------
# account settings
# --------------------------------------------------------------------------


@router.get("/me/account")
def account(request: Request, user: User = Depends(require_user)):
    return render(request, "account/account.html")


@router.post("/me/account/password")
def change_password(
    request: Request,
    current: str = Form(...),
    new: str = Form(...),
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    if not verify_password(current, user.password_hash):
        flash(request, "Your current password wasn't right.", "error")
        return redirect("/me/account")
    problem = password_problem(new, user.email)
    if problem:
        flash(request, problem, "error")
        return redirect("/me/account")
    user.password_hash = hash_password(new)
    user.session_version += 1
    _audit(session, user, "password_changed")
    session.commit()
    _sign_in(request, user)
    flash(request, "Password changed. Other devices have been signed out.")
    return redirect("/me/account")


@router.post("/me/account/delete")
def delete_account(request: Request, confirm: str = Form(""), user: User = Depends(require_user),
                   session: Session = Depends(get_session)):
    if confirm.strip().lower() != "delete":
        flash(request, "Type DELETE to confirm.", "error")
        return redirect("/me/account")
    _audit(session, None, "account_deleted", user.id)
    session.delete(user)
    session.commit()
    request.session.clear()
    flash(request, "Your account and everything in it has been deleted.")
    return redirect("/")
