"""
POST /api/v1/analyze

Accepts multipart (`media` or `image`, plus `tags`) or JSON (`media_base64` | `media_url`,
`image_*` still accepted, plus `tags`). Returns a per-tag verdict with a confidence score and
evidence.

An image and a video use the same endpoint and the same response shape. A video adds a `media`
block and a `frame` inside each verdict's evidence; an image response is unchanged, key for
key, from what it has always been.

The pipeline itself lives in tagverify/analyze/run.py, shared with the playground UI. This handler
only does HTTP: authenticate, parse, translate the outcome.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from tagverify.analyze import intake as intake_mod
from tagverify.analyze.run import AnalysisSuccess, run_analysis, write_audit
from tagverify.auth.authenticate import charge_extra
from tagverify.auth.deps import Guarded, guard
from tagverify.db.session import get_session
from tagverify.errors import ApiError
from tagverify.scoring.client import InferenceError, InferenceWarming

log = logging.getLogger(__name__)
router = APIRouter()


async def read_intake(request: Request) -> intake_mod.Intake:
    content_type = request.headers.get("content-type", "")

    if "multipart/form-data" in content_type:
        form = await request.form()
        # Any file part is accepted, `media` and `image` first. See upload_from_form() for
        # why this must not hinge on one exact field name.
        upload = intake_mod.upload_from_form(form)
        if upload is None:
            # Name the fields that DID arrive. "No file attached" plus the list is diagnosable
            # in one glance; the bare instruction that used to be here was not.
            raise intake_mod.MediaIntakeError(
                "NO_IMAGE",
                "No file was attached. Send it as `media` in a multipart/form-data body. "
                f"Fields received: {intake_mod.form_field_names(form)}.",
            )
        # Support both repeated `tags` fields and one comma-separated value.
        many = [str(value) for value in form.getlist("tags")]
        tags = intake_mod.normalise_tags(many if len(many) > 1 else (many[0] if many else ""))
        return intake_mod.build(await upload.read(), tags)

    if "application/json" in content_type:
        raw = await request.body()
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise intake_mod.MediaIntakeError("BAD_REQUEST", "Body is not valid JSON.") from exc
        if not isinstance(body, dict):
            raise intake_mod.MediaIntakeError("BAD_REQUEST", "Body must be a JSON object.")
        return await intake_mod.from_json(body)

    raise intake_mod.MediaIntakeError(
        "BAD_REQUEST", "Use multipart/form-data or application/json."
    )


@router.post("/analyze")
async def analyze(
    request: Request,
    background: BackgroundTasks,
    auth: Guarded = Depends(guard),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    try:
        intake = await read_intake(request)
    except intake_mod.MediaIntakeError as exc:
        raise ApiError(exc.code, exc.message) from exc

    try:
        outcome, audit = await run_analysis(
            session, intake=intake, api_key_id=auth.key.id
        )
    except InferenceWarming as exc:
        raise ApiError(
            "INFERENCE_WARMING",
            "The inference service is starting up. Retry shortly.",
            retry_after=exc.retry_after,
        ) from exc
    except InferenceError as exc:
        raise ApiError("INFERENCE_FAILED", str(exc)) from exc

    if not isinstance(outcome, AnalysisSuccess):
        raise ApiError(
            "UNKNOWN_TAG",
            f"Unknown tag(s): {', '.join(outcome.unknown)}",
            unknown_tags=outcome.unknown,
            known_tags=outcome.known,
        )

    if audit:
        background.add_task(write_audit, audit)

    # `guard` charged one unit before the body was read. A video that cost N model calls owes
    # N, not 1 — otherwise it is the cheap way to consume N times the capacity for the same
    # quota. Charged only for what was actually run, so a cache hit stays a single unit.
    if not outcome.cached and outcome.frames_analyzed > 1:
        await charge_extra(session, auth.key.id, outcome.frames_analyzed - 1)

    body = {
        "request_id": outcome.request_id,
        "image_hash": outcome.image_hash,
        "image_bytes": outcome.image_bytes,
        "model": outcome.model,
        "packs_version": outcome.packs_version,
        "decision_version": outcome.decision_version,
        "cached": outcome.cached,
        "latency_ms": outcome.latency_ms,
        "results": [verdict.to_api() for verdict in outcome.verdicts],
    }
    # Stated explicitly rather than buried: an uncalibrated tag's verdict is an educated
    # guess, and the caller deserves to know which ones those are. The key is omitted
    # entirely when the list is empty, so its presence is itself the signal.
    if outcome.uncalibrated:
        body["uncalibrated_tags"] = outcome.uncalibrated

    # Present only for video, for the same reason as above: an image response must stay
    # byte-identical to what existing integrations already parse, and the key's presence is
    # itself how a caller knows frames were involved.
    if outcome.kind == "video":
        body["media"] = {
            "kind": "video",
            "duration_s": outcome.duration_s,
            "frames_analyzed": outcome.frames_analyzed,
            # If this exceeds frames_analyzed, the cap truncated and coverage is partial.
            # Stated rather than left to be inferred from a frame count.
            "frames_considered": outcome.frames_considered,
        }

    return JSONResponse(body, headers=auth.headers())
