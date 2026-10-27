"""Job alert emails.

* Public alerts: anyone can subscribe with just an email address (double opt-in).
* Personal alerts: approved users get their new matches above their chosen score.

Digests go out once the local time is past 7am, daily or weekly.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import mailer
from .config import get_settings
from .models import AlertSubscription, Job, Match, Profile, User, utcnow

LOCAL_TZ = ZoneInfo("America/Los_Angeles")
SEND_AFTER_HOUR = 7
MAX_JOBS_PER_EMAIL = 25


def _due(last: datetime | None, frequency: str, now: datetime) -> bool:
    if frequency == "off":
        return False
    if now.astimezone(LOCAL_TZ).hour < SEND_AFTER_HOUR:
        return False
    if last is None:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    gap = timedelta(hours=22) if frequency == "daily" else timedelta(days=6, hours=22)
    return now - last >= gap


def subscription_matches(sub: AlertSubscription, job: Job) -> bool:
    if job.is_remote:
        if not sub.include_remote:
            return False
    elif sub.counties and job.county not in sub.counties:
        return False
    if sub.categories and job.category not in sub.categories:
        return False
    if sub.keywords:
        text = f"{job.title} {job.description or ''}".lower()
        words = [w for w in re.split(r"[,\s]+", sub.keywords.lower()) if len(w) > 1]
        if words and not any(re.search(rf"\b{re.escape(w)}", text) for w in words):
            return False
    return True


def send_public_alerts(session: Session, now: datetime | None = None) -> int:
    now = now or utcnow()
    sent = 0
    subs = session.scalars(
        select(AlertSubscription).where(
            AlertSubscription.confirmed_at.is_not(None), AlertSubscription.unsubscribed_at.is_(None)
        )
    )
    for sub in subs:
        if not _due(sub.last_sent_at, sub.frequency, now):
            continue
        since = sub.last_sent_at or sub.confirmed_at
        jobs = session.scalars(
            select(Job).where(Job.status == "open", Job.first_seen_at > since).order_by(Job.first_seen_at.desc())
        ).all()
        picked = [job for job in jobs if subscription_matches(sub, job)]
        if picked:
            settings = get_settings()
            mailer.send(
                sub.email,
                f"{len(picked)} new job{'s' if len(picked) != 1 else ''} — {settings.site_name}",
                "public_digest",
                jobs=picked[:MAX_JOBS_PER_EMAIL],
                more=max(0, len(picked) - MAX_JOBS_PER_EMAIL),
                sub=sub,
            )
            sent += 1
        sub.last_sent_at = now
    session.commit()
    return sent


def send_personal_alerts(session: Session, now: datetime | None = None) -> int:
    now = now or utcnow()
    sent = 0
    rows = session.execute(
        select(User, Profile).join(Profile, Profile.user_id == User.id).where(User.status == "active")
    ).all()
    for user, profile in rows:
        if not _due(profile.last_alert_at, profile.alert_frequency, now):
            continue
        matches = session.scalars(
            select(Match)
            .join(Job, Job.id == Match.job_id)
            .where(
                Match.user_id == user.id,
                Match.alerted_at.is_(None),
                Match.dismissed.is_(False),
                Match.score >= profile.alert_min_score,
                Job.status == "open",
            )
            .order_by(Match.score.desc())
        ).all()
        if matches:
            settings = get_settings()
            mailer.send(
                user.email,
                f"{len(matches)} new match{'es' if len(matches) != 1 else ''} for you — {settings.site_name}",
                "personal_digest",
                user=user,
                matches=matches[:MAX_JOBS_PER_EMAIL],
                more=max(0, len(matches) - MAX_JOBS_PER_EMAIL),
            )
            for match in matches:
                match.alerted_at = now
            sent += 1
        profile.last_alert_at = now
    session.commit()
    return sent


def send_due(session: Session) -> dict:
    return {"public": send_public_alerts(session), "personal": send_personal_alerts(session)}
