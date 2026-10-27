"""Runtime configuration, read from environment variables.

Everything that differs between a laptop and production lives here so the
rest of the code never calls os.getenv directly.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def _list(name: str, default: str = "") -> list[str]:
    raw = os.getenv(name, default) or ""
    return [item.strip() for item in raw.split(",") if item.strip()]


SIGNUP_MODES = ("approval", "open", "closed")


@dataclass(frozen=True)
class Settings:
    site_name: str = "Mother Lode Careers"
    site_tagline: str = "Local jobs in Calaveras, Amador & Tuolumne counties"
    public_base_url: str = "http://127.0.0.1:8000"
    environment: str = "development"  # development | production | test
    database_url: str = "sqlite:///./jobs.db"
    session_secret: str = "dev-only-change-me"

    # "approval": anyone may sign up, an admin approves before personal
    # features turn on.  "open": approved automatically.  "closed": no signups.
    signup_mode: str = "approval"
    admin_emails: list[str] = field(default_factory=list)

    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    smtp_starttls: bool = True

    openai_api_key: str | None = None
    openai_model: str = "gpt-5.6-luna"
    ai_scores_per_user_per_day: int = 25

    adzuna_app_id: str | None = None
    adzuna_app_key: str | None = None
    usajobs_api_key: str | None = None
    usajobs_user_agent: str | None = None

    http_user_agent: str = "MotherLodeCareersBot/1.0 (+community job board)"
    closed_job_grace_days: int = 30

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def email_enabled(self) -> bool:
        return bool(self.smtp_host and self.smtp_from)

    @property
    def ai_enabled(self) -> bool:
        return bool(self.openai_api_key)


@lru_cache
def get_settings() -> Settings:
    mode = (os.getenv("SIGNUP_MODE") or "approval").strip().lower()
    if mode not in SIGNUP_MODES:
        raise ValueError(f"SIGNUP_MODE must be one of {SIGNUP_MODES}, not {mode!r}")
    return Settings(
        site_name=os.getenv("SITE_NAME") or Settings.site_name,
        site_tagline=os.getenv("SITE_TAGLINE") or Settings.site_tagline,
        public_base_url=(os.getenv("PUBLIC_BASE_URL") or Settings.public_base_url).rstrip("/"),
        environment=os.getenv("ENVIRONMENT") or Settings.environment,
        database_url=os.getenv("DATABASE_URL") or Settings.database_url,
        session_secret=os.getenv("SESSION_SECRET") or Settings.session_secret,
        signup_mode=mode,
        admin_emails=[e.lower() for e in _list("ADMIN_EMAILS")],
        smtp_host=os.getenv("SMTP_HOST") or None,
        smtp_port=_int("SMTP_PORT", 587),
        smtp_username=os.getenv("SMTP_USERNAME") or None,
        smtp_password=os.getenv("SMTP_PASSWORD") or None,
        smtp_from=os.getenv("SMTP_FROM") or None,
        smtp_starttls=_bool("SMTP_STARTTLS", True),
        openai_api_key=os.getenv("OPENAI_API_KEY") or None,
        openai_model=os.getenv("OPENAI_MODEL") or Settings.openai_model,
        ai_scores_per_user_per_day=_int("AI_SCORES_PER_USER_PER_DAY", 25),
        adzuna_app_id=os.getenv("ADZUNA_APP_ID") or None,
        adzuna_app_key=os.getenv("ADZUNA_APP_KEY") or None,
        usajobs_api_key=os.getenv("USAJOBS_API_KEY") or None,
        usajobs_user_agent=os.getenv("USAJOBS_USER_AGENT") or None,
        http_user_agent=os.getenv("HTTP_USER_AGENT") or Settings.http_user_agent,
        closed_job_grace_days=_int("CLOSED_JOB_GRACE_DAYS", 30),
    )
