"""
FastAPI dependencies for the two authentication schemes.

`guard` authenticates a request AND charges it against the key's rate limit — one database
statement, see tagverify/auth/authenticate.py. Route handlers stay a straight line:

    @router.post("/analyze")
    async def analyze(auth: Guarded = Depends(guard)): ...
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from tagverify.auth.admin import AdminState, check_csrf, state
from tagverify.auth.authenticate import AuthenticatedKey, AuthOk, authenticate_and_charge
from tagverify.config import settings
from tagverify.db.session import get_session
from tagverify.errors import ApiError


@dataclass(slots=True)
class Guarded:
    key: AuthenticatedKey
    limit: int
    remaining: int

    def headers(self) -> dict[str, str]:
        return {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(self.remaining),
        }


def _presented_key(request: Request) -> str | None:
    """
    The key from `x-api-key`, or from `Authorization: Bearer` — plenty of HTTP clients make
    the latter the path of least resistance.
    """
    presented = request.headers.get("x-api-key")
    if presented:
        return presented
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


async def guard(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Guarded:
    presented = _presented_key(request)

    if not presented:
        raise ApiError("MISSING_KEY", "Provide your API key in the x-api-key header.")

    outcome = await authenticate_and_charge(session, presented)

    if not isinstance(outcome, AuthOk):
        # Same response for unknown and revoked — never confirm a key exists.
        raise ApiError("INVALID_KEY", "This API key is not valid.")

    # Note the charge happens BEFORE this check, so an over-limit request still increments
    # the counter and a client that keeps hammering keeps its own counter climbing. That is
    # deliberate; it makes abuse self-limiting rather than free to retry.
    if outcome.count > outcome.limit:
        raise ApiError(
            "RATE_LIMITED",
            f"Rate limit of {outcome.limit} requests/minute exceeded.",
            retry_after=outcome.reset_after,
            limit=outcome.limit,
        )

    return Guarded(
        key=outcome.key,
        limit=outcome.limit,
        remaining=max(0, outcome.limit - outcome.count),
    )


def admin_state(request: Request) -> AdminState:
    return state(request)


#: The one scope that exists. Adding a second is a two-line change here plus a line in the
#: admin keys form; the shape is general, the vocabulary is deliberately not.
TAGS_WRITE = "tags:write"


async def require_tags_write(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> None:
    """
    The gate on the tag catalog: an admin session, OR an API key scoped to write tags.

    Two callers with genuinely different shapes, so two ways in.

    Profile's own /admin is a browser holding a `dooh_admin` cookie, and it keeps working
    exactly as before — same session check, same double-submit CSRF. DOOH is a server holding
    a key, and for it CSRF is meaningless: the attack CSRF defends against is a browser being
    induced to send credentials it already has, and there is no browser and no ambient
    credential here.

    What has NOT changed is the property the old key-refusal was protecting. That refusal
    said: "There is exactly one key and DOOH holds it, so keying these routes would let the
    client that asks 'does this image contain alcohol?' rewrite what alcohol means." Still
    true, and still enforced — the analyze key has no scopes and is refused here. Editing the
    taxonomy needs a SECOND key issued for it. What changed is that "which key" is now a
    question the database can answer, instead of one the auth layer had no way to ask.

    Authenticating a key CHARGES its rate limit, deliberately. auth/keys.py records why there
    is no authenticate-only helper: it is a footgun that ships unthrottled endpoints. So tag
    writes are rate limited like everything else a key does, and the key's own limit is the
    ceiling. That is a change from the admin-session path, which is not limited at all.
    """
    if state(request) == "ok":
        if request.method not in ("GET", "HEAD") and not check_csrf(
            request.headers.get("x-csrf-token")
        ):
            raise ApiError("FORBIDDEN", "Missing or invalid CSRF token.")
        return

    presented = _presented_key(request)
    if not presented:
        raise ApiError(
            "FORBIDDEN",
            "This endpoint requires an admin session, or an API key scoped to write tags.",
        )

    outcome = await authenticate_and_charge(session, presented)
    if not isinstance(outcome, AuthOk):
        raise ApiError("INVALID_KEY", "This API key is not valid.")
    if outcome.count > outcome.limit:
        raise ApiError(
            "RATE_LIMITED",
            f"Rate limit of {outcome.limit} requests/minute exceeded.",
            retry_after=outcome.reset_after,
            limit=outcome.limit,
        )
    if TAGS_WRITE not in outcome.key.scopes:
        # Names the scope rather than saying "forbidden": the caller holds a valid key and
        # can act on this, and the alternative is an operator guessing which of their two
        # keys they sent.
        raise ApiError(
            "FORBIDDEN",
            f"This API key is not scoped to write tags. It needs the “{TAGS_WRITE}” scope.",
        )


def require_admin_api(request: Request) -> None:
    """
    The admin gate, for JSON routes.

    Same two checks as web.admin.require_admin — a valid `dooh_admin` session, plus the
    double-submit CSRF token on anything mutating — but it raises ApiError so the failure
    comes back through the {error, message} envelope. The web version raises NotAuthorised,
    which the handler in main.py renders as an HTML login page: correct for /admin, useless
    to something parsing JSON.

    Tag writes are deliberately NOT authorised by API key. There is exactly one key and DOOH
    holds it, so keying these routes would let the client that asks "does this image contain
    alcohol?" rewrite what alcohol means.
    """
    if state(request) != "ok":
        raise ApiError("FORBIDDEN", "This endpoint requires an admin session.")
    if request.method not in ("GET", "HEAD") and not check_csrf(
        request.headers.get("x-csrf-token")
    ):
        raise ApiError("FORBIDDEN", "Missing or invalid CSRF token.")


# ------------------------------------------------------- playground IP limiter
#
# The playground deliberately carries no API key: it is same-origin, and the alternatives are
# shipping a key to the browser or issuing one to ourselves. But that also meant anyone who
# could reach the public URL got unlimited free inference. A per-IP fixed window keeps the
# no-key design and closes that hole.
#
# In-process, so it resets on restart and is per-worker. That is the right size for the job —
# a shared store would be more accurate and is not worth a Redis dependency here.

_hits: dict[str, deque[float]] = defaultdict(deque)


def client_ip(request: Request) -> str:
    # X-Forwarded-For is only trustworthy behind a proxy we control; uvicorn populates
    # request.client from it when run with --proxy-headers.
    return request.client.host if request.client else "unknown"


def playground_rate_limit(request: Request) -> None:
    ip = client_ip(request)
    limit = settings().playground_rate_limit_per_min
    now = time.monotonic()

    bucket = _hits[ip]
    while bucket and now - bucket[0] > 60:
        bucket.popleft()

    if len(bucket) >= limit:
        raise ApiError(
            "RATE_LIMITED",
            f"The playground allows {limit} analyses per minute. Use an API key for higher volume.",
            retry_after=60,
        )
    bucket.append(now)
