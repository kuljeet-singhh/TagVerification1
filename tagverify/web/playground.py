"""
The playground: GET / and the HTMX fragment endpoints behind it.

WHY THIS DOES NOT GO THROUGH /api/v1/analyze
--------------------------------------------
The public route exists to authenticate and throttle EXTERNAL callers. Routing the
same-origin playground through it would mean either shipping an API key to the browser or
issuing one to ourselves — both worse than just calling the shared pipeline directly.

What it does share is `run_analysis()`, so caching, thresholds and the audit trail cannot
drift between the two entrypoints. `api_key_id` is None here: playground traffic is internal
and is not billed to anyone's key.

The trade-off is that this endpoint has no key to rate-limit against, which previously meant
anyone who could reach the public URL got unlimited free inference. It is now limited per IP
instead — see tagverify.auth.deps.playground_rate_limit.
"""

from __future__ import annotations

import base64
import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from tagverify.analyze import intake as intake_mod
from tagverify.analyze.run import AnalysisSuccess, run_analysis, write_audit
from tagverify.auth.deps import playground_rate_limit
from tagverify.db.session import get_session
from tagverify.scoring.client import (
    InferenceError,
    InferenceWarming,
    cached_inference_health,
    cached_tag_catalog,
)
from tagverify.tags.groups import group_tags
from tagverify.templating import render

log = logging.getLogger(__name__)
router = APIRouter()

DEFAULT_TAGS = ["alcohol"]


async def _shell_context() -> dict[str, Any]:
    """packs_version / model / health dot for the shell rail. Never raises."""
    try:
        health = await cached_inference_health()
        return {
            "packs_version": health.packs_version,
            "model": health.model,
            "health_class": "is-ok",
        }
    except InferenceWarming:
        return {"health_class": "is-warming"}
    except Exception:  # noqa: BLE001
        return {"health_class": "is-down"}


async def _catalog_context(selected: list[str]) -> dict[str, Any]:
    try:
        catalog = await cached_tag_catalog()
    except InferenceWarming as exc:
        return {"inference_down": True, "warming": True, "error": str(exc), "tag_groups": []}
    except (InferenceError, Exception) as exc:  # noqa: BLE001
        return {
            "inference_down": True,
            "warming": False,
            "error": str(exc),
            "local_hint": True,
            "tag_groups": [],
        }

    return {
        "inference_down": False,
        "tag_groups": group_tags([tag.model_dump() for tag in catalog.tags]),
        "selected": selected,
    }


def _requested_tags(request: Request) -> list[str]:
    """
    `?tags=alcohol,vaping` pre-selects the picker.

    The old build hardcoded ["alcohol"] with no way to share a selection, so
    "check this creative against alcohol and vaping" was not a link you could send.
    """
    raw = request.query_params.get("tags")
    if not raw:
        return DEFAULT_TAGS
    tags = [tag.strip() for tag in raw.split(",") if tag.strip()]
    return tags or DEFAULT_TAGS


@router.get("/", response_class=HTMLResponse)
async def playground(request: Request) -> HTMLResponse:
    selected = _requested_tags(request)
    context = {"selected": selected}
    context.update(await _shell_context())
    context.update(await _catalog_context(selected))
    return render(request, "pages/playground.html", context)


@router.get("/ui/catalog", response_class=HTMLResponse)
async def catalog_fragment(request: Request) -> HTMLResponse:
    """
    Polled by the warming panel until the Space answers.

    Returns the empty results state once inference is reachable, which stops the poll because
    the replacement fragment carries no hx-trigger. The tag picker itself is re-rendered by a
    full page load; this only has to say "we're live now".
    """
    context = await _catalog_context(_requested_tags(request))
    if context.get("inference_down"):
        return render(request, "partials/inference_down.html", context)
    return render(
        request,
        "partials/results_empty.html",
        context,
        # Tell the browser to reload so the picker is populated from the now-warm catalog.
        headers={"HX-Refresh": "true"},
    )


@router.post("/ui/analyze", response_class=HTMLResponse)
async def analyze_fragment(
    request: Request,
    background: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    _: None = Depends(playground_rate_limit),
) -> HTMLResponse:
    form = await request.form()
    upload = intake_mod.upload_from_form(form)
    tags_raw = [str(value) for value in form.getlist("tags")]

    def fail(message: str, *, warming: bool = False) -> HTMLResponse:
        return render(
            request,
            "partials/results_error.html",
            {"message": message, "retryable": warming},
        )

    if upload is None:
        # This used to read "Choose a creative to analyse." — which accuses the user of the
        # one thing they had already done, and sent three debugging sessions to the browser
        # instead of the server. Say what actually arrived, and name the likeliest cause.
        return fail(
            "No file reached the server. The upload may have been blocked, or this page may "
            f"be out of date — try a hard refresh. Fields received: "
            f"{intake_mod.form_field_names(form)}."
        )
    if not tags_raw:
        return fail("Pick at least one tag to verify.")

    data = await upload.read()
    try:
        intake = intake_mod.build(data, intake_mod.normalise_tags(tags_raw))
    except intake_mod.MediaIntakeError as exc:
        return fail(exc.message)

    try:
        outcome, audit = await run_analysis(session, intake=intake, api_key_id=None)
    except InferenceWarming:
        return fail(
            "The inference service is waking up — it sleeps after 48 hours idle and takes "
            "30–60 seconds to load the model.",
            warming=True,
        )
    except InferenceError as exc:
        return fail(f"Inference failed: {exc}")

    if not isinstance(outcome, AnalysisSuccess):
        return fail(
            f"Unknown tag(s): {', '.join(outcome.unknown)}. "
            "Nothing was checked — an unrecognised tag is never reported as absent."
        )

    if audit:
        background.add_task(write_audit, audit)

    grouped: dict[str, list] = {"present": [], "uncertain": [], "absent": []}
    for verdict in sorted(outcome.verdicts, key=lambda v: v.score, reverse=True):
        grouped[verdict.band].append(verdict)

    return render(
        request,
        "partials/results.html",
        {
            "result": outcome,
            "grouped": grouped,
            "counts": {band: len(items) for band, items in grouped.items()},
            # The preview is echoed back as a data URL so the evidence overlay has an image
            # to draw on without the server ever storing the creative. Only the sha256 is
            # persisted; see tagverify/db/models.py.
            #
            # Stills only. A video is up to 50MB and inlining that as a data URL would be
            # absurd — the browser already holds the file it just uploaded, so the video
            # preview is wired up client-side from its own object URL instead, and seeks
            # itself to the evidence timestamp. See static/js/playground.js.
            "preview_data_url": "data:image/jpeg;base64," + base64.b64encode(data).decode()
            if intake.kind == "image" and len(data) < 2_000_000
            else None,
            "media_kind": intake.kind,
            "duration_s": intake.duration_s,
        },
        headers={"HX-Trigger": "analysis:done"},
    )
