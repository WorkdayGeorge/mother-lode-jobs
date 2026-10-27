"""Passwords, one-time tokens and CSRF."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AuthToken, User, utcnow

_N, _R, _P = 2**14, 8, 1
MIN_PASSWORD = 10


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return "scrypt${}${}${}${}${}".format(
        _N, _R, _P, base64.b64encode(salt).decode(), base64.b64encode(digest).decode()
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        check = hashlib.scrypt(
            password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=32
        )
        return hmac.compare_digest(check, base64.b64decode(digest))
    except (ValueError, TypeError):
        return False


def password_problem(password: str, email: str = "") -> str | None:
    if len(password) < MIN_PASSWORD:
        return f"Use at least {MIN_PASSWORD} characters."
    if email and password.lower() == email.lower():
        return "Your password can't be your email address."
    return None


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def issue_token(session: Session, user: User, purpose: str, hours: int) -> str:
    """Create a single-use token; older unused ones for the same purpose stop working."""
    for old in session.scalars(
        select(AuthToken).where(AuthToken.user_id == user.id, AuthToken.purpose == purpose, AuthToken.used_at.is_(None))
    ):
        old.used_at = utcnow()
    raw = secrets.token_urlsafe(32)
    session.add(
        AuthToken(user_id=user.id, purpose=purpose, token_hash=_hash_token(raw), expires_at=utcnow() + timedelta(hours=hours))
    )
    return raw


def use_token(session: Session, raw: str, purpose: str, *, consume: bool = True) -> User | None:
    token = session.scalar(
        select(AuthToken).where(AuthToken.token_hash == _hash_token(raw or ""), AuthToken.purpose == purpose)
    )
    if token is None or token.used_at is not None or token.expires_at < utcnow():
        return None
    if consume:
        token.used_at = utcnow()
    return session.get(User, token.user_id)


def csrf_token(session_data: dict) -> str:
    token = session_data.get("csrf")
    if not token:
        token = secrets.token_urlsafe(24)
        session_data["csrf"] = token
    return token


def csrf_ok(session_data: dict, submitted: str | None) -> bool:
    expected = session_data.get("csrf")
    return bool(expected and submitted and hmac.compare_digest(expected, submitted))
