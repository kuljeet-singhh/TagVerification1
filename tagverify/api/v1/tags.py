"""
GET /api/v1/tags — the tag catalog, with a per-tag `calibrated` flag.

Thresholds are deliberately NOT exposed. Publishing them would both invite gaming (a caller
could tune a creative to sit just under a cutoff) and make them awkward to change, since
they would become part of the public contract rather than an implementation detail.
`calibrated` is exposed because it tells the caller how much to trust the verdict, which is
the caller's business.
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
from tagverify.tags.decide import cached_thresholds, decision_version

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
            # needs both: packs_version moves when the model's numbers change, this moves
            # when the rule or the cutoffs applied to them do. Keying a cache on only the
            # first is how a superseded refusal outlives the fix for it.
            "decision_version": decision_version(thresholds, registry.rule()),
            "count": len(catalog.tags),
            "tags": [
                {
                    "slug": tag,
                    "label": rows[tag].label,
                    "description": rows[tag].description,
                    "calibrated": bool(
                        (row := thresholds.get(tag))
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
# NOTE ON CACHES. These handlers deliberately do NOT call reset_caches(), and that is still
# right now that publishing no longer needs a restart. GET /tags above is served from the
# model tier's catalog, built from the pack the model currently holds. A row written here
# cannot appear there until the pack is PUSHED — writing to content_tags does not publish
# anything — so at this point there is still no stale cache to clear, and clearing one would
# imply an immediacy the write does not have. `pending publish` on the response tells the
# truth.
#
# The invalidation belongs to the publish, not to the write, and it lives there:
# scoring.client.push_packs() calls reset_caches() once the model confirms the swap. Doing it
# here as well would clear the memos at the moment nothing changed and leave them warm at the
# moment everything did.


class TagPayload(BaseModel):
    label: str
    description: str
    positives: list[str]
    negatives: list[str]
    rationale: str | None = None
    sigmoid_floor: float | None = None


class CreateTagPayload(TagPayload):
    slug: str


def _tag_json(row: ContentTag) -> dict[str, Any]:
    return {
        "slug": row.slug,
        "label": row.label,
        "description": row.description,
        "positives": row.positives,
        "negatives": row.negatives,
        "rationale": row.rationale,
        "sigmoid_floor": row.sigmoid_floor,
        "status": row.status,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "updated_by": row.updated_by,
    }


@router.get("/tags/managed", dependencies=[Depends(require_tags_write)])
async def managed_tags(
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """
    The catalog as stored, prompts included. Not the same thing as GET /tags.

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
