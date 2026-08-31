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
from tagverify.scoring.client import (
    InferenceError,
    InferenceWarming,
    PublishNotConfigured,
    cached_inference_health,
    cached_tag_catalog,
)
from tagverify.tags import catalog
from tagverify.tags.decide import cached_thresholds, decision_version
from tagverify.tags.publish import PackExportError, publish_catalog

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


async def _live_pack() -> tuple[set[str] | None, dict[str, str] | None]:
    """
    What the RUNNING model holds: its slugs, and its per-tag prompt fingerprints.

    `(None, None)` means "we could not ask" — the model is warming or unreachable. That is a
    real answer and NOT a failure: this endpoint is the admin tag list, and a model that is
    asleep must not empty the page. It also must not be reported as "nothing is published";
    publish_state() turns None into silence for exactly that reason.

    The same call and the same two-branch except as web/admin.py:_inference_context, which
    serves profile's own admin page from these facts. cached_inference_health() memoises
    SUCCESSES only, so a down model costs one timeout, not one per row.
    """
    try:
        health = await cached_inference_health()
    except (InferenceWarming, InferenceError):
        return None, None
    return set(health.tags), health.prompt_fingerprints


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
    rows = await catalog.list_tags(session)
    live_slugs, live_fingerprints = await _live_pack()
    return JSONResponse(
        {
            "count": len(rows),
            "tags": [
                {
                    **_tag_json(r),
                    "publish_state": catalog.publish_state(r, live_slugs, live_fingerprints),
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
        valid = await catalog.create_tag(session, **payload.model_dump())
    except catalog.TagValidationError as exc:
        raise ApiError("BAD_REQUEST", exc.message) from exc
    await session.commit()

    row = await catalog.get_tag(session, valid.slug)
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
    if await catalog.get_tag(session, slug) is None:
        raise ApiError("NOT_FOUND", f"No tag “{slug}”.")
    try:
        # Slug twice on purpose: positional picks the row, keyword is the value validate_tag
        # checks against it. TagPayload has no slug field precisely so the path owns it.
        valid = await catalog.update_tag(session, slug, slug=slug, **payload.model_dump())
    except catalog.TagValidationError as exc:
        raise ApiError("BAD_REQUEST", exc.message) from exc
    await session.commit()

    row = await catalog.get_tag(session, slug)
    assert row is not None
    return JSONResponse({**_tag_json(row), "warnings": valid.warnings, "pending_publish": True})


@router.delete("/tags/{slug}", dependencies=[Depends(require_tags_write)])
async def delete_tag(
    slug: str,
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """Retire, never delete — see catalog.retire_tag for what a hard delete would strand."""
    try:
        await catalog.retire_tag(session, slug)
    except catalog.TagValidationError as exc:
        raise ApiError("NOT_FOUND", exc.message) from exc
    await session.commit()

    # Re-read rather than reuse the instance: commit expires every attribute, and touching one
    # afterwards would lazy-load from a context that cannot await it.
    row = await catalog.get_tag(session, slug)
    assert row is not None
    return JSONResponse({**_tag_json(row), "pending_publish": True})


@router.post("/tags/publish", dependencies=[Depends(require_tags_write)])
async def publish_tags(session: AsyncSession = Depends(get_session)) -> JSONResponse:
    """
    Make every written tag live, without restarting the model.

    The remote half of `dooh export-packs --push`, and the same code underneath — this exports
    the catalog to the pack file and pushes those bytes into the running model, which
    re-encodes its prompt table in place.

    ONE ENDPOINT FOR THE WHOLE CATALOG, because a pack is one artifact with one fingerprint.
    There is no such thing as publishing a single tag, and an endpoint shaped like
    `/tags/{slug}/publish` would be lying about that. A consequence worth knowing rather than
    engineering around: this publishes every pending edit, including someone else's.

    It is also the only place reset_caches() belongs. The write handlers above deliberately
    skip it — nothing they do reaches the model — but this moves packs_version, and leaving
    the memos warm across it would stamp new-pack scores with the old fingerprint for up to
    their TTL.

    Not cheap: re-encoding the prompt table is seconds, not milliseconds, and it moves
    packs_version, so every tag reports calibrated: false until recalibrated. Batch edits and
    publish once.
    """
    try:
        health = await publish_catalog(session)
    except PackExportError as exc:
        raise ApiError("BAD_REQUEST", str(exc)) from exc
    except PublishNotConfigured as exc:
        # 409, not 502. Nothing upstream is broken and a retry changes nothing — the fix is
        # an operator setting RELOAD_SECRET on both tiers.
        raise ApiError("NOT_CONFIGURED", str(exc)) from exc
    except InferenceWarming as exc:
        raise ApiError(
            "INFERENCE_WARMING",
            "The model is starting up and cannot accept a pack yet. Retry shortly.",
            retry_after=exc.retry_after,
        ) from exc
    except InferenceError as exc:
        raise ApiError("INFERENCE_FAILED", str(exc)) from exc

    return JSONResponse(
        {
            "packs_version": health.packs_version,
            "count": len(health.tags),
            "tags": sorted(health.tags),
        }
    )
