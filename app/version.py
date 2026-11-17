"""Which build is actually running.

One place to answer "是不是已經套用最新版本". ``APP_VERSION`` is bumped by hand
when a release ships; ``COMMIT`` and ``STARTED_AT`` come from the running
process so a deployed instance can be checked against the repo without having
to trust (or even have) ``.git`` inside the container.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

APP_VERSION = "1.15.0"

# Bump this whenever APP_VERSION changes; the README keeps the full history.
RELEASED_AT = "2026-11-17"

_REPO = Path(__file__).resolve().parent.parent

# Railway sets these on every deploy; other hosts use their own names.
_COMMIT_ENV = ("RAILWAY_GIT_COMMIT_SHA", "GIT_COMMIT", "SOURCE_VERSION", "RENDER_GIT_COMMIT")


def _git_commit() -> str:
    """Short commit of the deployed code, or "" when it cannot be determined.

    Deliberately silent on failure: a missing commit should never stop the
    portal from booting, it just makes the version line shorter.
    """
    for key in _COMMIT_ENV:
        value = os.environ.get(key, "").strip()
        if value:
            return value[:7]

    try:
        head = (_REPO / ".git" / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref:"):
            ref = head.split(" ", 1)[1].strip()
            head = (_REPO / ".git" / ref).read_text(encoding="utf-8").strip()
        return head[:7]
    except (OSError, IndexError):
        return ""


COMMIT = _git_commit()

# Process start, which for a container is the deploy. Shown as "啟動時間" so it
# is not mistaken for the commit date. Railway runs on UTC; the people reading
# this are in Hong Kong, so the label uses the same zone as the rest of the app.
def _now_local() -> str:
    name = os.environ.get("APP_TZ", "Asia/Hong_Kong").strip() or "Asia/Hong_Kong"
    try:
        tz = ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        tz = None
    now = datetime.now(tz) if tz else datetime.now().astimezone()
    return now.strftime("%Y-%m-%d %H:%M")


STARTED_AT = _now_local()


def label() -> str:
    """'v1.1.0 · a8636a9' — what the UI puts in the corner."""
    return f"v{APP_VERSION} · {COMMIT}" if COMMIT else f"v{APP_VERSION}"


def as_dict() -> dict:
    """Everything /health reports, so a deploy can be verified with one curl."""
    return {
        "version": APP_VERSION,
        "released_at": RELEASED_AT,
        "commit": COMMIT,
        "started_at": STARTED_AT,
    }
