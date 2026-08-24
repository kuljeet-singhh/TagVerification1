"""
The analyze pipeline, shared by the public API route and the playground UI.

Both callers need identical behaviour — same cache, same thresholds, same audit trail — and
the only differences are authentication (the API charges a key, the UI does not) and how the
outcome is rendered. Keeping the pipeline here means a fix to caching or threshold handling
cannot apply to one path and not the other.

A video is the same pipeline with more than one frame: each frame is scored independently and
decided independently, and only then are the per-frame verdicts collapsed by
dooh/analyze/aggregate.py. Nothing between here and the model knows the difference — the Space
still only ever receives a single still.

Raises InferenceWarming / InferenceError; callers translate those into an HTTP status or a
UI message.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from dooh.analyze.aggregate import aggregate
from dooh.analyze.intake import Intake
from dooh.db.cache import find_cached, record_analysis
from dooh.inference.client import (
    InferenceError,
    analyze_image,
    cached_inference_health,
)
from dooh.tags.decide import (
    Thresholds,
    Verdict,
    cached_thresholds,
    decide_all,
    decision_version,
)

log = logging.getLogger(__name__)

# Ceiling on the whole media, on top of the per-call CALL_TIMEOUT_S in the inference client.
# Six frames each allowed the full 8s would otherwise let one request occupy a connection for
# the better part of a minute. Checked BETWEEN frames, so it never interrupts a call in
# flight — it just declines to start another one.
TOTAL_BUDGET_S = 30.0


@dataclass(slots=True)
class AnalysisSuccess:
    ok: bool = True
    request_id: str = ""
    image_hash: str = ""
    image_bytes: int = 0
    model: str = ""
    packs_version: str = ""
    #: Fingerprint of the rule and cutoffs that produced these verdicts. A caller caching
    #: them needs it in the key alongside packs_version, or a retuned threshold never
    #: reaches them — the retroactivity this service re-decides for stops at their door.
    decision_version: str = ""
    cached: bool = False
    latency_ms: int = 0
    verdicts: list[Verdict] = field(default_factory=list)
    #: "image" or "video". Drives whether the response carries a `media` block at all.
    kind: str = "image"
    duration_s: float | None = None
    frames_analyzed: int = 1
    frames_considered: int = 1
    #: Tags whose thresholds have never been calibrated against labelled data.
    uncalibrated: list[str] = field(default_factory=list)
    #: The thresholds each verdict was decided with. The UI renders these as a band ruler so
    #: a score is legible as "0.51 in a 0.30-0.55 uncertain band" rather than a bare number.
    #: Never returned by the public API — see dooh/api/v1/tags.py for why.
    thresholds: dict[str, Thresholds] = field(default_factory=dict)


@dataclass(slots=True)
class AnalysisRejected:
    ok: bool = False
    code: str = "UNKNOWN_TAG"
    unknown: list[str] = field(default_factory=list)
    known: list[str] = field(default_factory=list)


AnalysisOutcome = AnalysisSuccess | AnalysisRejected


async def run_analysis(
    session: AsyncSession,
    *,
    intake: Intake,
    #: None for the internal playground; a key id for public API traffic.
    api_key_id: str | None,
) -> tuple[AnalysisOutcome, dict[str, Any] | None]:
    """
    Returns (outcome, audit_row). The caller is responsible for writing `audit_row` as a
    background task — see the note below.
    """
    started_at = time.monotonic()
    tags = intake.tags

    # Memoised for 60s. The pack fingerprint is part of the cache key and the tag list gates
    # validation, so both are needed before anything else happens.
    health = await cached_inference_health()

    unknown = [tag for tag in tags if tag not in health.tags]
    if unknown:
        # Never silently drop an unknown tag: "we didn't check" must not be presented as
        # "we checked and it's clean". This is checked BEFORE any inference, so a typo costs
        # nothing and returns no partial results.
        return AnalysisRejected(unknown=unknown, known=list(health.tags)), None

    cached = await find_cached(session, intake.hash, tags, health.packs_version)
    thresholds = await cached_thresholds(session)

    raw: list[dict[str, Any]]
    if cached is not None:
        raw = cached.results
    else:
        raw = await _score_frames(intake)

    # Decide EVERY frame, then collapse. Deciding first is what lets the sigmoid floor veto a
    # frame before it can win on score alone; see dooh/analyze/aggregate.py.
    verdicts = aggregate(decide_all(raw, thresholds, health.packs_version))
    request_id = str(uuid.uuid4())
    latency_ms = int((time.monotonic() - started_at) * 1000)

    # The audit row is written by the CALLER, as a background task, so that a slow or failing
    # insert never costs the caller a result they have already waited for. Unlike the
    # previous implementation, `record_analysis` logs its failures rather than swallowing
    # them — losing the audit trail of a compliance tool silently is not acceptable.
    #
    # `results` holds EVERY frame's raw scores, not just the winning frame's. That is what
    # keeps the cache honest: which frame wins depends on the thresholds, so a cached video
    # re-read after a threshold change has to be re-aggregated from all of them. Storing only
    # the winner would silently freeze a video's verdict at the thresholds of the day it was
    # first analysed — exactly the retroactivity the cache exists to provide.
    audit = {
        "request_id": request_id,
        "api_key_id": api_key_id,
        "image_hash": intake.hash,
        "tags": tags,
        "results": raw,
        "packs_version": health.packs_version,
        "latency_ms": latency_ms,
        "cached": cached is not None,
    }

    # Frame count comes from the raw scores rather than from `intake`, so a cache hit reports
    # what was actually analysed rather than what this request happened to re-sample.
    analyzed = len({row.get("frame_index", 0) for row in raw}) or 1

    return (
        AnalysisSuccess(
            request_id=request_id,
            image_hash=intake.hash,
            image_bytes=intake.bytes,
            model=health.model,
            packs_version=health.packs_version,
            decision_version=decision_version(thresholds),
            cached=cached is not None,
            latency_ms=latency_ms,
            verdicts=verdicts,
            uncalibrated=[v.tag for v in verdicts if not v.calibrated],
            thresholds={v.tag: thresholds.get(v.tag) or Thresholds() for v in verdicts},
            kind=intake.kind,
            duration_s=intake.duration_s,
            frames_analyzed=analyzed,
            frames_considered=max(intake.frames_considered, analyzed),
        ),
        audit,
    )


async def _score_frames(intake: Intake) -> list[dict[str, Any]]:
    """
    Score every frame, annotating each raw verdict with where it came from.

    SEQUENTIAL, deliberately. The Space is a single queued process on two free CPU cores
    (demo.queue in inference/app.py), so issuing frames concurrently reorders the queue
    without shortening it, and costs us the ability to stop early on a failure.

    A FRAME THAT FAILS FAILS THE WHOLE REQUEST. There is no partial result here: returning
    verdicts computed over four of six frames, with nothing in the response saying so, is
    "we didn't check" presented as "we checked and it's clean" — the single failure mode this
    product exists to prevent. Both exceptions raised here are already handled by the callers
    as a retryable 503 or a 502.
    """
    raw: list[dict[str, Any]] = []
    started_at = time.monotonic()

    for frame in intake.frames:
        if frame.index > 0 and (time.monotonic() - started_at) > TOTAL_BUDGET_S:
            raise InferenceError(
                f"Exceeded the {TOTAL_BUDGET_S:.0f}s budget for this media after "
                f"{frame.index} of {len(intake.frames)} frames."
            )

        analysis = await analyze_image(frame.base64, intake.tags)
        for verdict in analysis.results:
            row = verdict.model_dump()
            # Only for video. An image's raw rows stay exactly the shape they have always
            # been, so a cache entry written before this change still decodes cleanly.
            if intake.kind == "video":
                row["frame_index"] = frame.index
                row["timestamp_s"] = frame.timestamp_s
            raw.append(row)

    return raw


async def write_audit(audit: dict[str, Any]) -> None:
    """Background-task entrypoint for the audit row produced by `run_analysis`."""
    await record_analysis(**audit)
