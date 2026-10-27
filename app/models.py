"""Database schema.

Jobs are shared and public. Everything personal (profile, matches, saved and
applied jobs, resumes) belongs to exactly one user.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Any

from .clock import local_today

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator):
    """Timezone-aware UTC datetimes on every database (SQLite drops the zone)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        if value is not None and dialect.name == "sqlite":
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


# --------------------------------------------------------------------------
# Sources and jobs
# --------------------------------------------------------------------------

SOURCE_KINDS = ("employer", "government", "aggregator", "remote")


class Source(Base):
    """One place jobs come from: an employer careers page, a county feed, an API.

    ``adapter`` names the code that knows how to read it (``neogov``,
    ``edjoin``, ``adp`` ...) and ``config`` holds that adapter's settings,
    e.g. ``{"agency": "amadorgov"}``.  Adding an employer that uses a known
    platform is a new row, not new code.
    """

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(80), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20), default="employer")
    adapter: Mapped[str] = mapped_column(String(40))
    config: Mapped[dict[str, Any]] = mapped_column(default=dict)
    careers_url: Mapped[str | None] = mapped_column(String(1000))
    county: Mapped[str | None] = mapped_column(String(60))
    default_category: Mapped[str | None] = mapped_column(String(80))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

    # health, updated after every run
    last_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_success_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_job_count: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    consecutive_empty: Mapped[int] = mapped_column(Integer, default=0)
    max_job_count: Mapped[int] = mapped_column(Integer, default=0)
    page_fingerprint: Mapped[str | None] = mapped_column(String(64))
    page_changed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    jobs: Mapped[list[Job]] = relationship(back_populates="source")

    @property
    def health(self) -> str:
        """ok | warning | failing | changed | new | paused"""
        if not self.active:
            return "paused"
        if self.last_run_at is None:
            return "new"
        if self.consecutive_failures >= 2:
            return "failing"
        if self.adapter == "page_watch":
            return "changed" if self.page_changed_at else "ok"
        if self.consecutive_failures == 1:
            return "warning"
        if self.consecutive_empty >= 3 and self.max_job_count > 0:
            return "warning"
        return "ok"


class SourceRun(Base):
    __tablename__ = "source_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), index=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    jobs_found: Mapped[int] = mapped_column(Integer, default=0)
    jobs_kept: Mapped[int] = mapped_column(Integer, default=0)
    jobs_new: Mapped[int] = mapped_column(Integer, default=0)
    jobs_closed: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)


JOB_STATUSES = ("open", "closed", "hidden")


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_job_source_external"),
        Index("ix_jobs_status_first_seen", "status", "first_seen_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id", ondelete="SET NULL"))
    external_id: Mapped[str] = mapped_column(String(300))
    slug: Mapped[str] = mapped_column(String(120))

    title: Mapped[str] = mapped_column(String(300))
    employer: Mapped[str] = mapped_column(String(200), index=True)
    employer_slug: Mapped[str] = mapped_column(String(120), index=True)
    location: Mapped[str | None] = mapped_column(String(300))
    town: Mapped[str | None] = mapped_column(String(80), index=True)
    county: Mapped[str | None] = mapped_column(String(60), index=True)
    is_remote: Mapped[bool] = mapped_column(Boolean, default=False)
    work_arrangement: Mapped[str] = mapped_column(String(20), default="onsite")
    category: Mapped[str | None] = mapped_column(String(80), index=True)
    employment_type: Mapped[str | None] = mapped_column(String(80))
    salary_text: Mapped[str | None] = mapped_column(String(200))
    salary_min: Mapped[float | None] = mapped_column(Float)
    salary_max: Mapped[float | None] = mapped_column(Float)
    salary_unit: Mapped[str | None] = mapped_column(String(20))
    description: Mapped[str | None] = mapped_column(Text)
    apply_url: Mapped[str] = mapped_column(String(2000))
    posted_on: Mapped[date | None] = mapped_column(Date)
    # Date used for "newest first": the employer's posting date when it's
    # believable, otherwise the day we first saw the job.
    listed_on: Mapped[date] = mapped_column(Date, default=local_today, index=True)
    closes_on: Mapped[date | None] = mapped_column(Date)
    attribution: Mapped[str | None] = mapped_column(String(200))
    extra: Mapped[dict[str, Any]] = mapped_column(default=dict)

    status: Mapped[str] = mapped_column(String(10), default="open")
    duplicate_of_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    content_hash: Mapped[str | None] = mapped_column(String(64))

    source: Mapped[Source | None] = relationship(back_populates="jobs")

    @property
    def is_open(self) -> bool:
        return self.status == "open"



# --------------------------------------------------------------------------
# People
# --------------------------------------------------------------------------

USER_STATUSES = ("unverified", "pending", "active", "denied", "suspended")


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(20), default="unverified")
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    signup_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    session_version: Mapped[int] = mapped_column(Integer, default=1)

    profile: Mapped[Profile | None] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )

    @property
    def is_active(self) -> bool:
        return self.status == "active"


class Profile(Base):
    """What a job seeker is looking for and what they bring."""

    __tablename__ = "profiles"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    target_roles: Mapped[list[Any]] = mapped_column(default=list)
    skills: Mapped[list[Any]] = mapped_column(default=list)
    experience_summary: Mapped[str | None] = mapped_column(Text)
    counties: Mapped[list[Any]] = mapped_column(default=list)
    towns: Mapped[list[Any]] = mapped_column(default=list)
    categories: Mapped[list[Any]] = mapped_column(default=list)
    include_remote: Mapped[bool] = mapped_column(Boolean, default=False)
    employment_types: Mapped[list[Any]] = mapped_column(default=list)
    min_hourly_pay: Mapped[float | None] = mapped_column(Float)
    must_avoid: Mapped[list[Any]] = mapped_column(default=list)

    resume_filename: Mapped[str | None] = mapped_column(String(300))
    resume_text: Mapped[str | None] = mapped_column(Text)
    resume_uploaded_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    alert_frequency: Mapped[str] = mapped_column(String(10), default="daily")  # off|daily|weekly
    alert_min_score: Mapped[int] = mapped_column(Integer, default=60)
    last_alert_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    user: Mapped[User] = relationship(back_populates="profile")

    @property
    def is_complete(self) -> bool:
        return bool((self.target_roles or self.skills or self.resume_text) and (self.counties or self.towns or self.include_remote))


class Match(Base):
    """A job scored against one user's profile."""

    __tablename__ = "matches"
    __table_args__ = (UniqueConstraint("user_id", "job_id", name="uq_match_user_job"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    score: Mapped[int] = mapped_column(Integer)
    method: Mapped[str] = mapped_column(String(10))  # quick | ai
    profile_version: Mapped[int] = mapped_column(Integer)
    summary: Mapped[str | None] = mapped_column(Text)
    strengths: Mapped[list[Any]] = mapped_column(default=list)
    gaps: Mapped[list[Any]] = mapped_column(default=list)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    alerted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    dismissed: Mapped[bool] = mapped_column(Boolean, default=False)

    job: Mapped[Job] = relationship()


TRACK_STATUSES = ("saved", "applied", "interview", "offer", "hired", "not_selected", "withdrawn")


class TrackedJob(Base):
    """A job the user saved or applied to, with their own notes."""

    __tablename__ = "tracked_jobs"
    __table_args__ = (UniqueConstraint("user_id", "job_id", name="uq_tracked_user_job"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(20), default="saved")
    applied_on: Mapped[date | None] = mapped_column(Date)
    follow_up_on: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    job: Mapped[Job] = relationship()


class AiUsage(Base):
    __tablename__ = "ai_usage"
    __table_args__ = (UniqueConstraint("user_id", "day", name="uq_ai_usage_day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    day: Mapped[date] = mapped_column(Date)
    count: Mapped[int] = mapped_column(Integer, default=0)


TOKEN_PURPOSES = ("verify_email", "reset_password")


class AuthToken(Base):
    __tablename__ = "auth_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    purpose: Mapped[str] = mapped_column(String(20))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


# --------------------------------------------------------------------------
# Public (no-account) features
# --------------------------------------------------------------------------


class AlertSubscription(Base):
    """Email-only job alert. No account, no password, double opt-in."""

    __tablename__ = "alert_subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), index=True)
    keywords: Mapped[str | None] = mapped_column(String(200))
    counties: Mapped[list[Any]] = mapped_column(default=list)
    categories: Mapped[list[Any]] = mapped_column(default=list)
    include_remote: Mapped[bool] = mapped_column(Boolean, default=False)
    frequency: Mapped[str] = mapped_column(String(10), default="weekly")  # daily|weekly
    token: Mapped[str] = mapped_column(String(64), unique=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    unsubscribed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    @property
    def is_live(self) -> bool:
        return self.confirmed_at is not None and self.unsubscribed_at is None


SUBMISSION_KINDS = ("careers_page", "single_job")
SUBMISSION_STATUSES = ("pending", "approved", "rejected")


class Submission(Base):
    """An employer telling us about their careers page or a single opening."""

    __tablename__ = "submissions"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))
    employer: Mapped[str] = mapped_column(String(200))
    contact_name: Mapped[str | None] = mapped_column(String(120))
    contact_email: Mapped[str] = mapped_column(String(320))
    url: Mapped[str | None] = mapped_column(String(2000))
    job_title: Mapped[str | None] = mapped_column(String(300))
    town: Mapped[str | None] = mapped_column(String(80))
    description: Mapped[str | None] = mapped_column(Text)
    pay: Mapped[str | None] = mapped_column(String(200))
    closes_on: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    review_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    actor_id: Mapped[str | None] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(60))
    target: Mapped[str | None] = mapped_column(String(200))
    detail: Mapped[str | None] = mapped_column(Text)


class WorkerRun(Base):
    __tablename__ = "worker_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    summary: Mapped[dict[str, Any]] = mapped_column(default=dict)
