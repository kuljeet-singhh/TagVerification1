"""
GET /api/v1/tags — the tag catalog, with a per-tag `calibrated` flag.

Thresholds are deliberately NOT exposed. Publishing them would both invite gaming (a caller
could tune a creative to sit just under a cutoff) and make them awkward to change, since
they would become part of the public contract rather than an implementation detail.
`calibrated` is exposed because it tells the caller how much to trust the verdict, which is
the caller's business.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from tagverify.auth.deps import Guarded, guard
from tagverify.db.session import get_session
from tagverify.errors import ApiError
from tagverify.scoring.client import InferenceError, InferenceWarming, cached_tag_catalog
from tagverify.tags.decide import cached_thresholds, decision_version

router = APIRouter()


@router.get("/tags")
async def tags(
    auth: Guarded = Depends(guard),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    try:
        catalog = await cached_tag_catalog()
    except InferenceWarming as exc:
        raise ApiError(
            "INFERENCE_WARMING",
            "The inference service is starting up. Retry shortly.",
            retry_after=exc.retry_after,
        ) from exc
    except InferenceError as exc:
        raise ApiError("INFERENCE_FAILED", str(exc)) from exc

    thresholds = await cached_thresholds(session)

    return JSONResponse(
        {
            "packs_version": catalog.packs_version,
            # The other half of "what produced this verdict". A caller caching our answers
            # needs both: packs_version moves when the model's numbers change, this moves
            # when the rule or the cutoffs applied to them do. Keying a cache on only the
            # first is how a superseded refusal outlives the fix for it.
            "decision_version": decision_version(thresholds),
            "count": len(catalog.tags),
            "tags": [
                {
                    "slug": tag.slug,
                    "label": tag.label,
                    "description": tag.description,
                    "calibrated": bool(
                        (row := thresholds.get(tag.slug))
                        and row.calibrated
                        and (
                            not row.packs_version_seen
                            or row.packs_version_seen == catalog.packs_version
                        )
                    ),
                }
                for tag in catalog.tags
            ],
        },
        headers=auth.headers(),
    )
