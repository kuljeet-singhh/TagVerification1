"""
Admin session for /admin.

The admin pages can mint and revoke API keys and change thresholds that decide compliance
verdicts, so they cannot be left open — a public /admin would let anyone issue themselves a
key.

This is a single shared password, not user accounts, which is proportionate for an internal
tool. The session cookie is HMAC-signed with the password itself, so it cannot be forged
without knowing it, and changing the password invalidates every existing session for free.

If this ever faces more than a handful of internal people, replace it with real accounts
rather than adding roles on top of a shared secret.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections import defaultdict, deque
from typing import Literal

from fastapi import Request, Response

from tagverify.config import settings

COOKIE = "dooh_admin"
MAX_AGE_SECONDS = 7 * 24 * 60 * 60

AdminState = Literal["ok", "locked", "unconfigured"]


def _secret() -> str | None:
    value = (settings().admin_password or "").strip()
    return value or None


def _sign(payload: str, key: str) -> str:
    return hmac.new(key.encode(), payload.encode(), hashlib.sha256).hexdigest()


def state(request: Request) -> AdminState:
    """
    "unconfigured" is reported distinctly from "locked" on purpose: without ADMIN_PASSWORD
    set there is nothing to authenticate against, and silently allowing access would be the
    dangerous failure mode. We refuse and say why.
    """
    key = _secret()
    if not key:
        return "unconfigured"

    raw = request.cookies.get(COOKIE)
    if not raw or "." not in raw:
        return "locked"

    issued_at, _, signature = raw.partition(".")
    if not issued_at or not signature:
        return "locked"

    try:
        age_ms = time.time() * 1000 - float(issued_at)
    except ValueError:
        return "locked"
    # A negative age means a future-dated cookie — reject rather than treating it as fresh.
    if age_ms < 0 or age_ms > MAX_AGE_SECONDS * 1000:
        return "locked"

    # Constant-time compare — a signature check is exactly where a timing leak would matter.
    return "ok" if hmac.compare_digest(_sign(issued_at, key), signature) else "locked"


def issue_cookie(response: Response, request: Request) -> None:
    key = _secret()
    if not key:
        return
    issued_at = str(int(time.time() * 1000))
    response.set_cookie(
        COOKIE,
        f"{issued_at}.{_sign(issued_at, key)}",
        httponly=True,
        samesite="lax",
        # Secure is taken from the actual request scheme rather than a guess about the
        # environment. Hard-coding it on breaks plain-HTTP localhost (the browser silently
        # drops the cookie and login appears to do nothing); hard-coding it off ships an
        # insecure cookie in production. Behind a proxy this needs
        # `--proxy-headers --forwarded-allow-ips=...` so the scheme is the client's, not the
        # proxy hop's.
        secure=request.url.scheme == "https",
        path="/",
        max_age=MAX_AGE_SECONDS,
    )


def clear_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE, path="/")


def check_password(password: str) -> AdminState:
    key = _secret()
    if not key:
        return "unconfigured"
    return "ok" if hmac.compare_digest(password, key) else "locked"


def csrf_token() -> str:
    """
    A token for the double-submit check on state-changing form posts.

    Next.js Server Actions carried an implicit origin check; plain form POSTs do not, so the
    port would otherwise open a hole that did not exist before. SameSite=Lax already blocks
    the cross-site case in current browsers — this is the second layer, not the only one.

    Derived from the admin password so it needs no separate secret and rotates with it.
    """
    key = _secret()
    return _sign("csrf", key) if key else ""


def check_csrf(token: str | None) -> bool:
    expected = csrf_token()
    if not expected:
        return False
    return bool(token) and hmac.compare_digest(token, expected)


# ------------------------------------------------------- login attempt throttle
#
# The previous implementation had no lockout at all, which made a shared password the whole
# of the defence. A fixed window per IP is enough to turn online guessing from "a script" back
# into "not worth it", without needing any storage.

_attempts: dict[str, deque[float]] = defaultdict(deque)


def too_many_attempts(client_ip: str) -> bool:
    limit = settings().admin_login_attempts_per_min
    now = time.monotonic()
    bucket = _attempts[client_ip]
    while bucket and now - bucket[0] > 60:
        bucket.popleft()
    return len(bucket) >= limit


def record_attempt(client_ip: str) -> None:
    _attempts[client_ip].append(time.monotonic())


def clear_attempts(client_ip: str) -> None:
    _attempts.pop(client_ip, None)
