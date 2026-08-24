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

from tagverify.auth.admin import AdminState, state
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


async def guard(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Guarded:
    presented = request.headers.get("x-api-key")
    if not presented:
        # Also accept `Authorization: Bearer <key>`, since plenty of HTTP clients make that
        # the path of least resistance.
        authorization = request.headers.get("authorization", "")
        if authorization.lower().startswith("bearer "):
            presented = authorization[7:].strip()

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
            f"The playground allows {limit} analyses per minute. "
            "Use an API key for higher volume.",
            retry_after=60,
        )
    bucket.append(now)
