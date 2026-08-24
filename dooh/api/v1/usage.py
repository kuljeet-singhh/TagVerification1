"""
GET /api/v1/usage — this key's traffic today.

Strictly scoped to the presented key: a caller can never see another key's traffic, and there
is no aggregate view here at all. Aggregates belong in /admin, behind the admin gate.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from dooh.auth.deps import Guarded, guard
from dooh.auth.keys import masked_key
from dooh.db.session import get_session
from dooh.db.usage import analyses_today, usage_today, utc_midnight

router = APIRouter()


@router.get("/usage")
async def usage(
    auth: Guarded = Depends(guard),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    requests = await usage_today(session, auth.key.id)
    analyses, cache_hits, avg_latency = await analyses_today(session, auth.key.id)

    return JSONResponse(
        {
            "key": {
                "name": auth.key.name,
                "masked": masked_key(auth.key.key_prefix),
                "rate_limit_per_min": auth.key.rate_limit_per_min,
            },
            "today": {
                "since": utc_midnight().isoformat().replace("+00:00", "Z"),
                "requests": requests,
                "analyses": analyses,
                "cache_hits": cache_hits,
                "avg_latency_ms": avg_latency,
            },
            "rate_limit": {"limit": auth.limit, "remaining": auth.remaining},
        },
        headers=auth.headers(),
    )
