"""
/admin — issue keys, revoke keys, edit the tag catalog.

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
from tagverify.config import settings
from tagverify.db.models import ApiKey, ContentTag
from tagverify.db.session import get_session
from tagverify.scoring import registry
from tagverify.tags import catalog
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
    content_tags = (
        (await session.execute(select(ContentTag).order_by(ContentTag.sort_order, ContentTag.slug)))
        .scalars()
        .all()
    )
    context = {
        "keys": list(keys),
        "content_tags": list(content_tags),
        # Which model is answering. The form's SHAPE no longer depends on it — a tag is a
        # name and a sentence whoever reads them — but the page still names the reader,
        # because "why did it say that?" is answered differently by different models.
        "scorer_label": registry.describe(),
        "scorer_model": registry.model_label(),
        # A half-configuration (SCORER=vlm with no API key) drops the phrase requirement while
        # leaving nothing able to score. Surfaced so that shows up here, on the page where a
        # tag is being written, rather than at the first upload.
        "scorer_configured": settings().scorer_target is not None,
    } | await _inference_context()

    return context


async def _inference_context() -> dict[str, Any]:
    """
    The shell rail's health dot.

    It used to ask the Space three questions -- which slugs are live, which prompt
    fingerprints it holds, is it warm -- so the tags panel could say whether an edit had been
    published. None of that survives a scorer that reads its prompt per request: a saved tag
    IS live, so the question has no content and the answer would be theatre.
    """
    return {"health_class": "is-ok" if settings().scorer_target else "is-down"}


# ----------------------------------------------------------------------- page


# The template hides every panel whose name is not the selected tab, so an unrecognised
# ?tab= would render an empty page rather than falling through to the keys panel.
ADMIN_TABS = ("keys", "tags")


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


# ----------------------------------------------------------------------- tags


@router.post("/admin/tags", response_class=HTMLResponse)
async def create_content_tag(
    request: Request,
    slug: str = Form(""),
    label: str = Form(""),
    description: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    require_admin(request)

    async def panel(**extra: Any) -> HTMLResponse:
        # slug_preview so a re-rendered form shows the same identifier the author was looking
        # at when they pressed the button, rather than resetting to a dash beside a filled name.
        context = await _load(session)
        return render(
            request,
            "partials/tags_panel.html",
            context | {"slug_preview": catalog.slugify(label)} | extra,
        )

    # The RAW strings, exactly as typed. A re-render must not reformat someone's text
    # underneath them while they are still editing it.
    submitted = {"slug": slug, "label": label, "description": description}

    try:
        valid = await catalog.create_tag(
            session,
            # Blank means DERIVE, and blank is the normal case: the form shows the identifier
            # rather than asking for it, and only sends one when someone opened the override.
            # No extra state to carry -- an empty string is the whole signal.
            slug=slug.strip() or catalog.slug_from_label(label),
            label=label,
            description=description,
        )
    except catalog.TagValidationError as exc:
        # 200 with the panel re-rendered, never a 4xx: HTMX drops those, and the author would
        # watch a full form do nothing.
        #
        # And the form comes back POPULATED, override included. The description is the prompt
        # and is the part worth labouring over; making someone retype a considered sentence
        # because the name collided is how you get a careless one, which is the outcome this
        # validation exists to prevent. An identifier that survived a rejection has been seen
        # and possibly corrected by hand, so the template reopens the override rather than
        # burying it at the moment it matters most.
        await session.rollback()
        return await panel(tag_error=exc.message, form=submitted)

    await session.commit()

    # The warnings are the point of the form, not decoration. A tag saves either way — these
    # are heuristics, and refusing on a heuristic would be worse than advising on one — but
    # they name the specific thing most likely to make this tag misfire.
    return await panel(tag_warnings=valid.warnings, saved_tag=valid.slug)


@router.get("/admin/tags/slug-preview", response_class=HTMLResponse)
async def slug_preview(request: Request, label: str = "") -> HTMLResponse:
    """
    The identifier a name would produce, rendered as you type.

    SERVER-SIDE ON PURPOSE, for one round trip per keystroke-burst. Deriving it in JavaScript
    would be a third copy of `catalog.slugify` -- the two it replaces were both JS -- and a
    preview that disagrees with what the save actually writes is worse than showing nothing.
    The slug is permanent and cannot be changed after creation, so the preview is the only
    moment anyone gets to notice it is wrong.

    A GET, so no CSRF token: it reads nothing and writes nothing. Still admin-gated, because
    the whole panel is.
    """
    require_admin(request)
    return render(
        request,
        "partials/slug_preview.html",
        {"slug": catalog.slugify(label), "label": label},
    )


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


@router.post("/admin/tags/{slug}/restore", response_class=HTMLResponse)
async def restore_content_tag(
    request: Request,
    slug: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """
    Put a retired tag back. The mirror of `retire_content_tag`, down to the failure handling.

    ADMIN ONLY, deliberately: `api/v1/tags.py` gets no matching endpoint. Its DELETE is
    documented to dooh-backend as a retire, and adding a public un-retire is a contract change
    that service would need to know about first.
    """
    require_admin(request)
    try:
        await catalog.restore_tag(session, slug)
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
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """
    Rewriting the DESCRIPTION is the accuracy loop — it is the whole reason edit exists rather
    than delete-and-recreate, which would strand the slug's labelled eval images and its
    measured thresholds. It used to be rewriting the hard negatives; those went with migration
    0003, and the sentence took over the job.

    An edit here moves `packs_version`, because the description is half of what
    `catalog_fingerprint` hashes. That is correct and is the point: verdicts decided against
    the old wording should not be served under the new one.
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
        )
    except catalog.TagValidationError as exc:
        await session.rollback()
        # Back into the EDIT form, not the display row: the author's text has to survive a
        # rejection rather than being retyped from memory.
        return render(
            request,
            "partials/tag_edit_row.html",
            {"row": await catalog.get_tag(session, slug), "row_error": exc.message},
        )

    await session.commit()
    return await _tag_row(request, session, slug, row_warnings=valid.warnings)
