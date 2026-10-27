import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.update(
    ENVIRONMENT="test",
    DATABASE_URL="sqlite:///:memory:",
    SESSION_SECRET="test-secret",
    ADMIN_EMAILS="admin@example.com",
    SIGNUP_MODE="approval",
    PUBLIC_BASE_URL="https://jobs.example.org",
)
for key in ("OPENAI_API_KEY", "SMTP_HOST", "ADZUNA_APP_ID", "ADZUNA_APP_KEY"):
    os.environ.pop(key, None)

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app import db, mailer  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.models import Base  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    url = os.getenv("TEST_DATABASE_URL")  # e.g. a throwaway PostgreSQL database
    if url:
        engine = create_engine(url, future=True)
        Base.metadata.drop_all(engine)
    else:
        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True
        )
    Base.metadata.create_all(engine)
    db.set_engine(engine)
    mailer.OUTBOX.clear()
    yield engine
    engine.dispose()


@pytest.fixture
def session(fresh_db):
    s = db.SessionLocal()
    yield s
    s.close()


@pytest.fixture
def settings_override(monkeypatch):
    """Change settings for one test: settings_override(signup_mode='open')."""
    from dataclasses import replace

    def apply(**changes):
        new = replace(get_settings(), **changes)
        for module in ("app.config", "app.web.core", "app.web.routes_public", "app.web.routes_account",
                       "app.web.routes_admin", "app.mailer", "app.matching", "app.alerts"):
            mod = sys.modules.get(module)
            if mod is not None and hasattr(mod, "get_settings"):
                monkeypatch.setattr(mod, "get_settings", lambda new=new: new)
        return new

    return apply


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.web.main import app

    with TestClient(app) as c:
        yield c


def csrf_from(client, path="/signup") -> str:
    html = client.get(path).text
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, f"no csrf token on {path}"
    return match.group(1)


def last_link(pattern: str) -> str:
    for message in reversed(mailer.OUTBOX):
        match = re.search(pattern, message.text)
        if match:
            return match.group(0)
    raise AssertionError(f"No email contained {pattern}")
