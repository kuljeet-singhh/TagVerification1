"""
Authenticate a key AND charge its rate limit in ONE database statement.

WHY THIS IS ONE STATEMENT
-------------------------
Two reasons, and only the first one has gone away.

The historical reason: the Neon HTTP driver sent each query as a separate HTTPS request, so
latency was (number of queries x round-trip time) — measured at 560-800ms per trip
cross-region. Folding two queries into one halved the auth cost of every request. With a
pooled connection that saving is now microseconds rather than half a second.

The reason it stays: it is still the only race-free way to do this. A read-then-write would
let two concurrent requests both observe count=59 against a limit of 60 and both proceed. The
CTE does the lookup, the increment and the last-used touch atomically, and RETURNING gives us
the post-increment count.

`k` finds the key, `bump` increments the counter only if `k` matched, `touched` updates
last_used_at, and the final select returns the key plus its new count.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from dooh.auth.keys import KEY_PREFIX, sha256_hex

log = logging.getLogger(__name__)


@dataclass(slots=True)
class AuthenticatedKey:
    id: str
    name: str
    key_prefix: str
    rate_limit_per_min: int


@dataclass(slots=True)
class AuthOk:
    key: AuthenticatedKey
    count: int
    limit: int
    reset_after: int


@dataclass(slots=True)
class AuthFailed:
    reason: str = "invalid"


AuthOutcome = AuthOk | AuthFailed

_QUERY = text(
    """
    with k as (
      select id, name, key_prefix, rate_limit_per_min
      from api_keys
      where key_hash = :key_hash and revoked_at is null
    ),
    bump as (
      insert into usage_counters (api_key_id, window_start, count)
      select k.id, :window_start, 1 from k
      on conflict (api_key_id, window_start)
        do update set count = usage_counters.count + 1
      returning api_key_id, count
    ),
    touched as (
      update api_keys set last_used_at = :now
      where id in (select id from k)
      returning id
    )
    select k.id, k.name, k.key_prefix, k.rate_limit_per_min,
           coalesce(bump.count, 1) as count
    from k left join bump on bump.api_key_id = k.id
    """
)


_TOP_UP = text(
    """
    insert into usage_counters (api_key_id, window_start, count)
    values (:api_key_id, :window_start, :extra)
    on conflict (api_key_id, window_start)
      do update set count = usage_counters.count + :extra
    """
)


async def charge_extra(session: AsyncSession, api_key_id: str, extra: int) -> None:
    """
    Charge additional units against a key's current window, after the fact.

    `guard` charges exactly one unit per HTTP request, because it runs as a dependency and
    the body has not been read yet — it cannot know how much work the request will turn out
    to be. For a still that is right. For a video it is not: one request that scores six
    frames costs six times the model time of one that scores a still, and billing them the
    same makes a video the cheap way to consume six times the capacity.

    So the frame count is charged here, once it is known. The window is recomputed rather
    than passed in: a request that straddles a minute boundary should charge the window it
    finishes in, and charging a window that has already rolled over would be free.

    Deliberately best-effort. The caller has already paid for and received a real answer, and
    a failed counter update must not turn that into an error — the same reasoning that makes
    the audit-log write a background task. It is logged rather than swallowed.
    """
    if extra < 1:
        return

    now = datetime.now(UTC)
    try:
        await session.execute(
            _TOP_UP,
            {
                "api_key_id": api_key_id,
                "window_start": now.replace(second=0, microsecond=0),
                "extra": extra,
            },
        )
    except Exception:
        log.exception("failed to charge %d extra unit(s) to key %s", extra, api_key_id)


async def authenticate_and_charge(session: AsyncSession, presented: str | None) -> AuthOutcome:
    # Cheap structural reject before touching the database at all. Note this leaks only
    # "that is not shaped like one of our keys", which is already public information.
    if not presented or not presented.startswith(KEY_PREFIX):
        return AuthFailed()

    now = datetime.now(UTC)
    # Fixed one-minute window, truncated. Fixed windows allow a burst across the boundary
    # (up to 2x the limit in a pathological case); this is abuse protection for a free-tier
    # service, not billing enforcement.
    window_start = now.replace(second=0, microsecond=0)

    row = (
        await session.execute(
            _QUERY,
            {
                "key_hash": sha256_hex(presented.strip()),
                "window_start": window_start,
                "now": now,
            },
        )
    ).first()

    if row is None:
        return AuthFailed()

    key = AuthenticatedKey(
        id=str(row[0]),
        name=str(row[1]),
        key_prefix=str(row[2]),
        rate_limit_per_min=int(row[3]),
    )
    seconds_left = (window_start.timestamp() + 60) - now.timestamp()

    return AuthOk(
        key=key,
        count=int(row[4]),
        limit=key.rate_limit_per_min,
        reset_after=max(1, math.ceil(seconds_left)),
    )
