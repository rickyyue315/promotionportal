"""Session auth with two roles.

Deliberately minimal: one password per role, held in environment variables, and
a signed cookie carrying the role and the person's display name. There is no
user table to administer and no password reset flow — for an internal tool with
a handful of buyers this is the right amount of machinery.

Set on Railway:
    SECRET_KEY      long random string
    ADMIN_PASSWORD  for 後台 (店舖 / 佔比)
    USER_PASSWORD   for 前台 (上載 / 計算 / 匯出)
"""

from __future__ import annotations

import hmac
import os
from typing import Optional

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

COOKIE_NAME = "pp_session"
MAX_AGE_SECONDS = 60 * 60 * 12

SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-secret-change-me")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin")
USER_PASSWORD = os.environ.get("USER_PASSWORD", "user")

ROLE_ADMIN = "admin"
ROLE_USER = "user"

_serializer = URLSafeTimedSerializer(SECRET_KEY, salt="pp-session")


class AuthRequired(Exception):
    """Raised when a route needs a session that the request does not have."""


def authenticate(password: str) -> Optional[str]:
    """Return the role for a password, or None."""
    if not password:
        return None
    if hmac.compare_digest(password, ADMIN_PASSWORD):
        return ROLE_ADMIN
    if hmac.compare_digest(password, USER_PASSWORD):
        return ROLE_USER
    return None


def issue_token(role: str, name: str) -> str:
    return _serializer.dumps({"role": role, "name": name or role})


def read_token(token: str) -> Optional[dict]:
    try:
        return _serializer.loads(token, max_age=MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return None


def current_user(request) -> Optional[dict]:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    data = read_token(token)
    if not data or data.get("role") not in (ROLE_ADMIN, ROLE_USER):
        return None
    return data


def require_user(request) -> dict:
    user = current_user(request)
    if not user:
        raise AuthRequired()
    return user


def require_admin(request) -> dict:
    user = current_user(request)
    if not user or user.get("role") != ROLE_ADMIN:
        raise AuthRequired()
    return user


def is_admin(user: Optional[dict]) -> bool:
    return bool(user) and user.get("role") == ROLE_ADMIN
