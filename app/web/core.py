"""Shared web helpers: templates, rendering, auth dependencies, CSRF, rate limits."""
from __future__ import annotations

from app.clock import local_date, local_today
import logging
import re
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.categories import CATEGORY_NAMES, EMPLOYMENT_TYPES
from app.config import get_settings
from app.db import get_session
from app.models import User
from app.region import COUNTIES, PRIMARY_COUNTIES, TOWNS
from app.security import csrf_ok, csrf_token
from app.text import slugify, truncate

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))


# --------------------------------------------------------------------------
# template helpers
# --------------------------------------------------------------------------


def _ago(value) -> str:
    from datetime import datetime

    if value is None:
        return ""
    if isinstance(value, datetime):
        value = local_date(value)
    days = (local_today() - value).days
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 14:
        return f"{days} days ago"
    if days < 60:
        return f"{days // 7} weeks ago"
    return value.strftime("%b %-d, %Y")


def _date(value, fmt="%b %-d, %Y") -> str:
    return value.strftime(fmt) if value else ""


def _county_short(name: str | None) -> str:
    return (name or "").replace(" County", "")


templates.env.filters.update(ago=_ago, date=_date, truncate_words=truncate, slug=slugify, county_short=_county_short)
templates.env.globals.update(
    CATEGORY_NAMES=CATEGORY_NAMES,
    EMPLOYMENT_TYPES=EMPLOYMENT_TYPES,
    PRIMARY_COUNTIES=PRIMARY_COUNTIES,
    ALL_COUNTIES=list(COUNTIES),
    TOWNS=TOWNS,
)


def flash(request: Request, message: str, kind: str = "ok") -> None:
    request.session.setdefault("flash", []).append({"m": message, "k": kind})


def render(request: Request, name: str, status_code: int = 200, **context: Any) -> HTMLResponse:
    settings = get_settings()
    context.update(
        settings=settings,
        user=getattr(request.state, "user", None),
        csrf=lambda: csrf_token(request.session),
        flashes=request.session.pop("flash", []),
        path=request.url.path,
    )
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


# --------------------------------------------------------------------------
# dependencies
# --------------------------------------------------------------------------


def load_user(request: Request, session: Session) -> User | None:
    uid = request.session.get("uid")
    if not uid:
        return None
    user = session.get(User, uid)
    if user is None or request.session.get("sv") != user.session_version or user.status in ("denied", "suspended"):
        request.session.pop("uid", None)
        return None
    return user


def current_user(request: Request, session: Session = Depends(get_session)) -> User | None:
    user = load_user(request, session)
    request.state.user = user
    return user


class LoginRequired(Exception):
    pass


def require_user(user: User | None = Depends(current_user)) -> User:
    """Signed in (any status except denied/suspended)."""
    if user is None:
        raise LoginRequired()
    return user


def require_active(user: User = Depends(require_user)) -> User:
    """Signed in and approved."""
    if not user.is_active:
        raise HTTPException(status_code=403, detail="pending")
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    if not (user.is_admin and user.is_active):
        raise HTTPException(status_code=404)
    return user


async def csrf_protect(request: Request) -> None:
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        form = await request.form()
        if not csrf_ok(request.session, form.get("csrf_token")):
            raise HTTPException(status_code=400, detail="Your form expired. Go back, refresh the page and try again.")


_hits: dict[str, deque] = defaultdict(deque)


def rate_limit(bucket: str, limit: int, seconds: int):
    """Per-IP limiter for forms anyone can submit (per server instance)."""

    def check(request: Request) -> None:
        if request.method != "POST" or get_settings().environment == "test":
            return
        ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "?").split(",")[0]
        key, now = f"{bucket}:{ip}", time.monotonic()
        queue = _hits[key]
        while queue and now - queue[0] > seconds:
            queue.popleft()
        if len(queue) >= limit:
            raise HTTPException(status_code=429, detail="Too many attempts. Wait a few minutes and try again.")
        queue.append(now)

    return check


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def notify_admins(session: Session, subject: str) -> None:
    from sqlalchemy import select

    from app import mailer

    settings = get_settings()
    emails = set(settings.admin_emails) | set(
        session.scalars(select(User.email).where(User.is_admin.is_(True), User.status == "active"))
    )
    for email in emails:
        mailer.send(email, subject, "admin_notice", message=subject, link=f"{settings.public_base_url}/admin")
