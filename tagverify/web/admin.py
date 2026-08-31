"""
/admin — issue keys, revoke keys, edit the tag catalog, edit thresholds.

AUTHORISATION LIVES ON EVERY HANDLER, NOT ON THE PAGE
-----------------------------------------------------
Each POST below is independently reachable; guarding only the GET that renders the form
would leave them callable by anyone who knows the URL. So every one of them re-checks the
session, and none of them relies on having been reached from a rendered page.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tagverify.auth import admin as auth
from tagverify.auth.deps import TAGS_WRITE, client_ip
from tagverify.auth.keys import create_api_key, revoke_api_key
from tagverify.db.models import ApiKey, ContentTag, TagThreshold
from tagverify.db.session import get_session
from tagverify.db.thresholds import upsert_threshold
from tagverify.scoring.client import (
    InferenceError,
    InferenceWarming,
    cached_inference_health,
)
from tagverify.tags import catalog
from tagverify.tags.decide import invalidate_threshold_cache
from tagverify.templating import render

log = logging.getLogger(__name__)
router = APIRouter()


class NotAuthorised(Exception):
    pass


def require_admin(request: Request) -> None:
    if auth.state(request) != "ok":
        raise NotAuthorised
    # The double-submit token. SameSite=Lax already blocks the cross-site POST in current
    # browsers; this is the second layer, and it costs one inherited hx-headers attribute.
    if request.method == "POST" and not auth.check_csrf(request.headers.get("x-csrf-token")):
        raise NotAuthorised


async def _load(session: AsyncSession) -> dict[str, Any]:
    keys = (
        (await session.execute(select(ApiKey).order_by(ApiKey.created_at.desc()))).scalars().all()
    )
    thresholds = (
        (await session.execute(select(TagThreshold).order_by(TagThreshold.slug))).scalars().all()
    )
    content_tags = (
        (await session.execute(select(ContentTag).order_by(ContentTag.sort_order, ContentTag.slug)))
        .scalars()
        .all()
    )
    context = {
        "keys": list(keys),
        "thresholds": list(thresholds),
        "content_tags": list(content_tags),
    } | await _inference_context()

    # Split here rather than in the template: Jinja cannot append to a list without the `do`
    # extension, and the banner needs the two groups counted before it renders either.
    buckets: dict[str, list[ContentTag]] = {"absent": [], "edited": []}
    for row in context["content_tags"]:
        if row.status != "active":
            continue
        state = catalog.publish_state(row, context["live_slugs"], context["live_fingerprints"])
        if state in buckets:
            buckets[state].append(row)
    return context | {"unpublished": buckets["absent"], "edited": buckets["edited"]}


async def _inference_context() -> dict[str, Any]:
    """
    What the RUNNING model knows: `live_slugs` for the tag rows, and the shell rail's
    health dot, pack fingerprint and model name.

    `live_slugs` is the slugs the model actually has, or None when we cannot find out.

    This is what lets the tags panel say whether an edit is live instead of warning about it
    unconditionally. A permanently-on warning is one people learn to scroll past, which is
    exactly how a tag came to sit in the database unpublished with nothing pointing at it.

    Deliberately asks the model rather than reading inference/packs.json. Two reasons:

    1. The pack file is NOT in the production image -- the Dockerfile copies only banding.py
       and versioning.py out of inference/ -- so reading it would work locally and raise
       FileNotFoundError on a deployed instance.
    2. The model's own catalog covers both ways a tag can be missing: the export never ran, or
       it ran and the process was never restarted. The remedy is the same either way.

    That second point used to end "so one comparison at this granularity is honest and two
    would be false precision", and it was wrong. Slugs only answer whether the model has HEARD
    of a tag. A tag that is edited keeps its slug, so rewriting every phrase in saloon left the
    panel completely silent while the model went on matching the phrases it was given at
    startup. `live_fingerprints` is the second comparison that paragraph talked itself out of:
    slug -> digest of the phrases and floor the model actually holds, so an unpublished EDIT
    shows up too. It is None on an older model tier that does not report them, and then we
    claim nothing about edits while still flagging genuinely absent slugs -- a partial answer,
    honestly scoped.

    None means "the model did not answer", and the caller must then claim nothing. Marking
    every tag as not-live because health timed out would be the same lie in a new coat.

    The rail's health dot rides along rather than getting its own call. /admin used to set no
    health_class at all, so base.html fell through to its "Inference unknown" default on every
    admin page load -- a word that carried no information while the rows beside it, from this
    same health answer, said "not live". But cached_inference_health() memoises SUCCESSES only,
    on purpose, so asking twice would cost two 8s timeouts on one page load in exactly the
    situation where the model is down. One call, both answers.
    """
    try:
        health = await cached_inference_health()
    except InferenceWarming:
        return {"live_slugs": None, "live_fingerprints": None, "health_class": "is-warming"}
    except InferenceError:
        return {"live_slugs": None, "live_fingerprints": None, "health_class": "is-down"}
    return {
        "live_slugs": set(health.tags),
        "live_fingerprints": health.prompt_fingerprints,
        "health_class": "is-ok",
        "packs_version": health.packs_version,
        "model": health.model,
    }


# ----------------------------------------------------------------------- page


# The template hides every panel whose name is not the selected tab, so an unrecognised
# ?tab= would render an empty page rather than falling through to the keys panel.
ADMIN_TABS = ("keys", "thresholds", "tags")


@router.get("/admin", response_class=HTMLResponse)
async def admin_page(
    request: Request, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    current = auth.state(request)
    if current == "unconfigured":
        return render(request, "pages/admin_unconfigured.html", {})
    if current == "locked":
        return render(request, "pages/admin_login.html", {})

    context = await _load(session)
    tab = request.query_params.get("tab", "keys")
    context |= {
        "tab": tab if tab in ADMIN_TABS else "keys",
        "csrf_token": auth.csrf_token(),
    }
    return render(request, "pages/admin.html", context)


# ---------------------------------------------------------------- session


@router.post("/admin/login")
async def login(request: Request, password: str = Form("")) -> Response:
    ip = client_ip(request)

    # The previous implementation had no lockout at all, which left a single shared password
    # as the whole of the defence against online guessing.
    if auth.too_many_attempts(ip):
        return render(
            request,
            "pages/admin_login.html",
            {"error": "Too many attempts. Wait a minute and try again."},
            status_code=429,
        )

    outcome = auth.check_password(password)
    if outcome == "unconfigured":
        return render(request, "pages/admin_unconfigured.html", {})
    if outcome != "ok":
        auth.record_attempt(ip)
        return render(
            request, "pages/admin_login.html", {"error": "Incorrect password."}, status_code=401
        )

    auth.clear_attempts(ip)
    response = RedirectResponse("/admin", status_code=303)
    auth.issue_cookie(response, request)
    return response


@router.post("/admin/logout")
async def logout() -> Response:
    response = RedirectResponse("/admin", status_code=303)
    auth.clear_cookie(response)
    return response


# -------------------------------------------------------------------- keys


@router.post("/admin/keys", response_class=HTMLResponse)
async def create_key(
    request: Request,
    name: str = Form(""),
    rate_limit: int = Form(60),
    tags_write: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    require_admin(request)

    context = await _load(session)
    name = name.strip()

    # A user-fixable problem comes back as 200 with the panel re-rendered and the message
    # beside the field, not as a 4xx that HTMX would drop on the floor.
    if not name:
        return render(
            request, "partials/keys_panel.html", context | {"key_error": "Give the key a name."}
        )
    if not 1 <= rate_limit <= 10_000:
        return render(
            request,
            "partials/keys_panel.html",
            context | {"key_error": "The rate limit must be between 1 and 10000."},
        )

    # An unchecked checkbox sends nothing at all, so absence is the default and the default
    # is analyze-only. Nothing here can widen an EXISTING key: scopes are set at issue and
    # there is no edit path, so broadening one means issuing a new key and revoking the old.
    scopes = [TAGS_WRITE] if tags_write else []
    issued = await create_api_key(session, name, rate_limit, scopes)
    await session.commit()

    context = await _load(session)
    return render(
        request,
        "partials/keys_panel.html",
        context | {"new_key": {"name": name, "plaintext": issued.plaintext}},
    )


@router.post("/admin/keys/{key_id}/revoke", response_class=HTMLResponse)
async def revoke_key(
    request: Request, key_id: str, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    require_admin(request)
    await revoke_api_key(session, key_id)
    await session.commit()
    return render(request, "partials/keys_panel.html", await _load(session))


# -------------------------------------------------------------- thresholds


@router.post("/admin/thresholds/{slug}", response_class=HTMLResponse)
async def save_threshold(
    request: Request,
    slug: str,
    low: float = Form(...),
    high: float = Form(...),
    floor: float = Form(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    require_admin(request)

    async def row_response(error: str | None = None, saved: bool = False) -> HTMLResponse:
        row = (
            await session.execute(select(TagThreshold).where(TagThreshold.slug == slug))
        ).scalar_one_or_none()
        return render(
            request,
            "partials/threshold_row.html",
            {"row": row, "row_error": error, "saved_slug": slug if saved else None},
        )

    for label, value in (("low", low), ("high", high), ("floor", floor)):
        if not 0 <= value <= 1:
            return await row_response(f"{label} must be between 0 and 1.")

    # An inverted band would make a score satisfy both "absent" and "present", leaving the
    # ORDER of the checks in banding.py to silently decide the verdict.
    if low > high:
        return await row_response("low must not be greater than high.")

    await upsert_threshold(session, slug, low=low, high=high, floor=floor)

    # Clear the 30s memo so the edit lands on the very next request rather than up to half a
    # minute later — the person who made the change should see it immediately. This stays in
    # the handler on purpose: the memo is process state, not database state.
    invalidate_threshold_cache()
    return await row_response(saved=True)


# ----------------------------------------------------------------------- tags


def _phrases(raw: str) -> list[str]:
    """One phrase per line. A textarea is the right control here — these are sentences."""
    return [line.strip() for line in raw.splitlines() if line.strip()]


@router.post("/admin/tags", response_class=HTMLResponse)
async def create_content_tag(
    request: Request,
    slug: str = Form(""),
    label: str = Form(""),
    description: str = Form(""),
    positives: str = Form(""),
    negatives: str = Form(""),
    rationale: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    require_admin(request)

    async def panel(**extra: Any) -> HTMLResponse:
        return render(request, "partials/tags_panel.html", (await _load(session)) | extra)

    # The RAW strings, not _phrases() output. Re-joining a parsed list would drop the author's
    # blank lines and reformat their text underneath them while they are still editing it.
    submitted = {
        "slug": slug,
        "label": label,
        "description": description,
        "positives": positives,
        "negatives": negatives,
        "rationale": rationale,
    }

    try:
        valid = await catalog.create_tag(
            session,
            slug=slug,
            label=label,
            description=description,
            positives=_phrases(positives),
            negatives=_phrases(negatives),
            rationale=rationale,
        )
    except catalog.TagValidationError as exc:
        # 200 with the panel re-rendered, never a 4xx: HTMX drops those, and the author would
        # watch a full form do nothing.
        #
        # And the form comes back POPULATED. A good tag carries 8+ positives and 10+ negatives,
        # hand-written and deliberately mirrored against each other; making someone retype all
        # of that because they were four phrases short is how you get four lazy phrases, which
        # is the outcome this validation exists to prevent. The edit form already did this.
        await session.rollback()
        return await panel(tag_error=exc.message, form=submitted)

    await session.commit()

    # The warnings are the point of the form, not decoration. A tag saves either way — these
    # are heuristics, and refusing on a heuristic would be worse than advising on one — but
    # they name the specific thing most likely to make this tag misfire.
    return await panel(tag_warnings=valid.warnings, saved_tag=valid.slug)


@router.post("/admin/tags/{slug}/retire", response_class=HTMLResponse)
async def retire_content_tag(
    request: Request,
    slug: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    require_admin(request)
    try:
        await catalog.retire_tag(session, slug)
        await session.commit()
    except catalog.TagValidationError as exc:
        await session.rollback()
        return render(
            request, "partials/tags_panel.html", (await _load(session)) | {"tag_error": exc.message}
        )
    return render(request, "partials/tags_panel.html", await _load(session))


async def _tag_row(
    request: Request, session: AsyncSession, slug: str, **extra: Any
) -> HTMLResponse:
    row = await catalog.get_tag(session, slug)
    # live_slugs must travel with the row. Without it the template cannot tell "not live" from
    # "we do not know", and a row swapped back after an edit would claim every tag is stale.
    return render(
        request,
        "partials/tag_row.html",
        {"row": row} | await _inference_context() | extra,
    )


@router.get("/admin/tags/{slug}/edit", response_class=HTMLResponse)
async def edit_content_tag(
    request: Request,
    slug: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Swap one row for its edit form. GET, but still admin-only — it exposes the prompts."""
    require_admin(request)
    row = await catalog.get_tag(session, slug)
    if row is None:
        return render(request, "partials/tags_panel.html", await _load(session))
    return render(request, "partials/tag_edit_row.html", {"row": row})


@router.get("/admin/tags/{slug}/row", response_class=HTMLResponse)
async def content_tag_row(
    request: Request,
    slug: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Cancel: swap the edit form back for the display row, discarding the edits."""
    require_admin(request)
    return await _tag_row(request, session, slug)


@router.post("/admin/tags/{slug}", response_class=HTMLResponse)
async def save_content_tag(
    request: Request,
    slug: str,
    label: str = Form(""),
    description: str = Form(""),
    positives: str = Form(""),
    negatives: str = Form(""),
    rationale: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """
    Rewriting the negatives is the accuracy loop — it is the whole reason edit exists rather
    than delete-and-recreate, which would strand the slug's labelled eval images and its
    measured thresholds.
    """
    require_admin(request)

    row = await catalog.get_tag(session, slug)
    if row is None:
        return render(request, "partials/tags_panel.html", await _load(session))

    try:
        # Twice on purpose, and not a typo: the positional says which row to load, the
        # keyword is the submitted slug that validate_tag compares against it to refuse a
        # rename. update_tag's `/` is what keeps them from colliding.
        valid = await catalog.update_tag(
            session,
            slug,
            slug=slug,
            label=label,
            description=description,
            positives=_phrases(positives),
            negatives=_phrases(negatives),
            rationale=rationale,
        )
    except catalog.TagValidationError as exc:
        await session.rollback()
        # Back into the EDIT form, not the display row: the author's text has to survive a
        # rejection, or a long prompt list is retyped from memory.
        return render(
            request,
            "partials/tag_edit_row.html",
            {"row": await catalog.get_tag(session, slug), "row_error": exc.message},
        )

    await session.commit()
    return await _tag_row(request, session, slug, row_warnings=valid.warnings)
