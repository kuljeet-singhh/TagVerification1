"""
/admin — issue keys, revoke keys, edit thresholds.

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
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from tagverify.auth import admin as auth
from tagverify.auth.deps import client_ip
from tagverify.auth.keys import create_api_key, revoke_api_key
from tagverify.db.models import ApiKey, TagThreshold
from tagverify.db.session import get_session
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
        (await session.execute(select(ApiKey).order_by(ApiKey.created_at.desc())))
        .scalars()
        .all()
    )
    thresholds = (
        (await session.execute(select(TagThreshold).order_by(TagThreshold.slug)))
        .scalars()
        .all()
    )
    return {"keys": list(keys), "thresholds": list(thresholds)}


# ----------------------------------------------------------------------- page


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
    context |= {"tab": request.query_params.get("tab", "keys"), "csrf_token": auth.csrf_token()}
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

    issued = await create_api_key(session, name, rate_limit)
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

    # Upsert, not update. The previous implementation only ever UPDATEd, so a tag present in
    # packs.json with no row here silently no-opped — the admin pressed Save, nothing
    # happened, and no error appeared anywhere.
    await session.execute(
        text(
            """
            insert into tag_thresholds
              (slug, threshold_low, threshold_high, sigmoid_floor,
               calibrated, precision, recall, updated_at, updated_by)
            values (:slug, :low, :high, :floor, false, null, null, now(), 'admin')
            on conflict (slug) do update set
              threshold_low = :low,
              threshold_high = :high,
              sigmoid_floor = :floor,
              -- Hand-editing means these are no longer the calibrated numbers.
              calibrated = false,
              precision = null,
              recall = null,
              updated_at = now(),
              updated_by = 'admin'
            """
        ),
        {"slug": slug, "low": low, "high": high, "floor": floor},
    )
    await session.commit()

    # Clear the 30s memo so the edit lands on the very next request rather than up to half a
    # minute later — the person who made the change should see it immediately.
    invalidate_threshold_cache()
    return await row_response(saved=True)
