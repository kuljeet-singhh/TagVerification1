"""
Rate-limit counter reads and maintenance.

The counter INCREMENT does not live here — it is folded into the authentication query in
dooh/auth/authenticate.py, so that verifying a key and charging its quota happen in one
statement and cannot race. This file holds only the reads and the sweeper.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from dooh.db.session import session_scope

log = logging.getLogger(__name__)


def utc_midnight() -> datetime:
    now = datetime.now(UTC)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


async def usage_today(session: AsyncSession, api_key_id: str) -> int:
    """Calls made by a key since UTC midnight — powers GET /api/v1/usage."""
    row = (
        await session.execute(
            text(
                """
                select coalesce(sum(count), 0)::int
                from usage_counters
                where api_key_id = :key and window_start >= :since
                """
            ),
            {"key": api_key_id, "since": utc_midnight()},
        )
    ).first()
    return int(row[0]) if row else 0


async def analyses_today(session: AsyncSession, api_key_id: str) -> tuple[int, int, int]:
    """(analyses, cache_hits, avg_latency_ms) for this key since UTC midnight."""
    row = (
        await session.execute(
            text(
                """
                select count(*)::int,
                       count(*) filter (where cached)::int,
                       coalesce(round(avg(latency_ms)), 0)::int
                from analyses
                where api_key_id = :key and created_at >= :since
                """
            ),
            {"key": api_key_id, "since": utc_midnight()},
        )
    ).first()
    if row is None:
        return (0, 0, 0)
    return (int(row[0]), int(row[1]), int(row[2]))


async def prune_usage_counters() -> int:
    """
    Drop counter rows older than a day.

    Nothing depends on them once the window has passed. The previous implementation exported
    this and never called it, so `usage_counters` grew without bound; it is now wired to both
    `dooh.cli prune-usage` and a daily task in the app lifespan.
    """
    async with session_scope() as session:
        result = await session.execute(
            text("delete from usage_counters where window_start < now() - interval '1 day'")
        )
        deleted = result.rowcount or 0
    if deleted:
        log.info("pruned %d expired usage_counters rows", deleted)
    return deleted
