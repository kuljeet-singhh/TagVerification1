"""
GET /api/v1/tags — the tag catalog, with a per-tag `calibrated` flag.

`calibrated` is exposed because it tells the caller how much to trust the verdict, which is
the caller's business, and it is the ONE number here that is honest about its own limits --
AGENTS.md rule 4. It is computed, never read raw: a calibration measured against a different
`packs_version` is stale and reports false. See decide.effective_calibrated.

There are no thresholds left to expose (migration 0004), but rule 7 stands for anything that
replaces them: a cutoff a caller can read is a cutoff a creative can be tuned to sit under.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from tagverify.auth.deps import Guarded, guard, require_tags_write
from tagverify.db.models import ContentTag
from tagverify.db.session import get_session
from tagverify.errors import ApiError
from tagverify.scoring import registry
from tagverify.tags import catalog as catalog_mod
from tagverify.tags.decide import (
    FALLBACK,
    cached_thresholds,
    decision_version,
    effective_calibrated,
)

router = APIRouter()


@router.get("/tags")
async def tags(
    auth: Guarded = Depends(guard),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    # From Postgres, not from a model. The catalog IS the prompt now, so the database is the
    # authority and there is no second copy to drift from it — which is what the
    # INFERENCE_WARMING and INFERENCE_FAILED branches here used to guard against.
    catalog = await registry.module().health(session)
    thresholds = await cached_thresholds(session)
    rows = {r.slug: r for r in await catalog_mod.list_tags(session, include_retired=False)}

    return JSONResponse(
        {
            "packs_version": catalog.packs_version,
            # The other half of "what produced this verdict". A caller caching our answers
            # needs both: packs_version moves when the question changes, this moves when the
            # rule that reads the answer does. Keying a cache on only the first is how a
            # superseded refusal outlives the fix for it.
            "decision_version": decision_version(thresholds, registry.rule()),
            "count": len(catalog.tags),
            "tags": [
                {
                    "slug": tag,
                    "label": rows[tag].label,
                    "description": rows[tag].description,
                    # Through the shared helper, not re-derived here. This comparison was
                    # written out inline three times and the admin panel's copy disagreed with
                    # this one for every stale calibration -- see decide.effective_calibrated.
                    "calibrated": effective_calibrated(
                        thresholds.get(tag) or FALLBACK, catalog.packs_version
                    ),
                }
                for tag in catalog.tags
            ],
        },
        headers=auth.headers(),
    )


# ------------------------------------------------------------------ management
#
# Create / read / update / retire over `content_tags`. Gated by require_tags_write: profile's
# own /admin session, or an API key carrying the `tags:write` scope.
#
# The unscoped analyze key is still refused, which is the point. There is one such key and
# DOOH holds it; letting the client that asks "does this contain alcohol?" rewrite what
# alcohol means was the thing to prevent, and it still is. Scopes did not open that door, they
# made "which key" answerable so a SECOND key could be issued for the catalog.
#
# NOTE ON CACHES. These handlers still call no invalidation, but the REASON has inverted and
# it is worth being explicit, because the old reason is the sort that outlives its mechanism.
# It used to be that a write could not reach the model until the pack was pushed, so there was
# nothing yet to invalidate. Now there is no pack: `scoring/gemini.py:health` re-reads
# content_tags on every request and rebuilds `packs_version` from it, so a row written here is
# live at the next call and no memo holds a stale catalog to clear.
#
# What that leaves is `pending_publish: True` on these responses, which is now a formality —
# there is no publish step for it to be pending. It is still emitted because dooh-backend
# reads it (`content-verification/client.ts` toWriteResult), and flipping it is a change to
# what that admin UI tells its user, not a cleanup to make in passing.


class TagPayload(BaseModel):
    """
    A tag as a caller may write it: a name, and the sentence the model is asked.

    The NAME is the required half, because it is the only half the model sees.

    `positives`, `negatives`, `rationale` and `sigmoid_floor` went with migration 0003. They
    are not rejected, they are IGNORED -- pydantic drops unknown fields by default, which is
    deliberate here: dooh-backend still sends all four (`content-verification/client.ts`
    toWirePayload), and a 422 would break its tag admin over data nothing has read since
    SigLIP was removed. It can drop them on its own schedule.
    """

    label: str
    #: Optional, matching the admin form. It is not sent to the model and a screen owner's
    #: picker renders nothing when it is blank, so there is nothing to refuse a write over.
    #: All three layers agree on this now -- dooh-backend dropped the @MinLength(1) it used to
    #: carry here (`content-tag.dto.ts`), and its admin form no longer marks the field required.
    #: There is no divergence left to go looking for.
    description: str = ""


class CreateTagPayload(TagPayload):
    slug: str


def _tag_json(row: ContentTag) -> dict[str, Any]:
    return {
        "slug": row.slug,
        "label": row.label,
        "description": row.description,
        "status": row.status,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "updated_by": row.updated_by,
    }


@router.get("/tags/managed", dependencies=[Depends(require_tags_write)])
async def managed_tags(
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """
    The catalog as stored, retired rows included. Not the same thing as GET /tags.

    Each row carries `publish_state`, because a row here is a database row and nothing more
    until someone publishes it. Without it a caller cannot tell a tag the model is scoring
    from one it has never heard of, and DOOH's admin page showed the two identically.

    Deliberately computed here and not stored: see docs/PORTING_THE_TAG_CATALOG.md §5. A
    persisted `published_at` records what we THINK we published, which diverges from what the
    process is actually serving at exactly the moment that matters.
    """
    rows = await catalog_mod.list_tags(session)
    return JSONResponse(
        {
            "count": len(rows),
            "tags": [
                {
                    **_tag_json(r),
                }
                for r in rows
            ],
        }
    )


@router.post("/tags", status_code=201, dependencies=[Depends(require_tags_write)])
async def create_tag(
    payload: CreateTagPayload,
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    try:
        valid = await catalog_mod.create_tag(session, **payload.model_dump())
    except catalog_mod.TagValidationError as exc:
        raise ApiError("BAD_REQUEST", exc.message) from exc
    await session.commit()

    row = await catalog_mod.get_tag(session, valid.slug)
    assert row is not None
    return JSONResponse(
        {**_tag_json(row), "warnings": valid.warnings, "pending_publish": True},
        status_code=201,
    )


@router.patch("/tags/{slug}", dependencies=[Depends(require_tags_write)])
async def patch_tag(
    slug: str,
    payload: TagPayload,
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    if await catalog_mod.get_tag(session, slug) is None:
        raise ApiError("NOT_FOUND", f"No tag “{slug}”.")
    try:
        # Slug twice on purpose: positional picks the row, keyword is the value validate_tag
        # checks against it. TagPayload has no slug field precisely so the path owns it.
        valid = await catalog_mod.update_tag(session, slug, slug=slug, **payload.model_dump())
    except catalog_mod.TagValidationError as exc:
        raise ApiError("BAD_REQUEST", exc.message) from exc
    await session.commit()

    row = await catalog_mod.get_tag(session, slug)
    assert row is not None
    return JSONResponse({**_tag_json(row), "warnings": valid.warnings, "pending_publish": True})


@router.delete("/tags/{slug}", dependencies=[Depends(require_tags_write)])
async def delete_tag(
    slug: str,
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """Retire, never delete — see catalog_mod.retire_tag for what a hard delete would strand."""
    try:
        await catalog_mod.retire_tag(session, slug)
    except catalog_mod.TagValidationError as exc:
        raise ApiError("NOT_FOUND", exc.message) from exc
    await session.commit()

    # Re-read rather than reuse the instance: commit expires every attribute, and touching one
    # afterwards would lazy-load from a context that cannot await it.
    row = await catalog_mod.get_tag(session, slug)
    assert row is not None
    return JSONResponse({**_tag_json(row), "pending_publish": True})
