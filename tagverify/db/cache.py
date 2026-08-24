"""
Result cache, keyed on (image bytes, tag set, packs version).

The cache stores RAW scores, not verdicts. That distinction is the whole point: scores are
the expensive part (a model forward pass) and never change for the same image and prompts,
while verdicts are a cheap comparison against thresholds that we expect to retune often. So
we cache the expensive half and re-decide on every read — meaning a threshold change takes
effect immediately, even for images already in the cache.

`packs_version` is part of the key because editing a prompt changes the scores. Without it, a
pack rewrite would serve stale numbers forever.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tagverify.db.models import Analysis
from tagverify.db.session import session_scope

log = logging.getLogger(__name__)


@dataclass(slots=True)
class CachedAnalysis:
    request_id: str
    results: list[dict[str, Any]]
    packs_version: str | None


def tag_key(tags: list[str]) -> list[str]:
    """Sorted, so tag order never fragments the cache."""
    return sorted(tags)


async def find_cached(
    session: AsyncSession,
    image_hash: str,
    tags: list[str],
    packs_version: str,
) -> CachedAnalysis | None:
    wanted = json.dumps(tag_key(tags))

    row = (
        await session.execute(
            text(
                """
                select request_id, results, packs_version
                from analyses
                where image_hash = :image_hash
                  and packs_version = :packs_version
                  -- Compare as sorted JSON arrays so tag order doesn't fragment the cache.
                  and (select coalesce(jsonb_agg(t order by t), '[]'::jsonb)
                       from jsonb_array_elements_text(tags_requested) as t) = (:wanted)::jsonb
                order by created_at desc
                limit 1
                """
            ),
            {"image_hash": image_hash, "packs_version": packs_version, "wanted": wanted},
        )
    ).first()

    if row is None:
        return None
    return CachedAnalysis(request_id=row[0], results=row[1], packs_version=row[2])


async def record_analysis(
    *,
    request_id: str,
    api_key_id: str | None,
    image_hash: str,
    tags: list[str],
    results: list[dict[str, Any]],
    packs_version: str,
    latency_ms: int,
    cached: bool,
) -> None:
    """
    Write the audit row.

    Runs as a background task so a logging failure never costs the caller a result they have
    already waited for — but unlike the previous implementation, a failure is LOGGED rather
    than swallowed. Silently losing the audit trail of a compliance tool is not an acceptable
    failure mode, even if it must not be a blocking one.

    Note that a cache HIT still writes a row, so this table grows per request rather than per
    unique image. That is intentional: the row is an audit record of a request being answered,
    not a record of an inference being run.
    """
    try:
        async with session_scope() as session:
            session.add(
                Analysis(
                    request_id=request_id,
                    api_key_id=api_key_id,
                    image_hash=image_hash,
                    tags_requested=tag_key(tags),
                    results=results,
                    packs_version=packs_version,
                    latency_ms=latency_ms,
                    cached=cached,
                )
            )
    except Exception:
        log.exception("failed to record analysis %s in the audit log", request_id)
