"""
The analyze pipeline, shared by the public API route and the playground UI.

Both callers need identical behaviour — same cache, same thresholds, same audit trail — and
the only differences are authentication (the API charges a key, the UI does not) and how the
outcome is rendered. Keeping the pipeline here means a fix to caching or threshold handling
cannot apply to one path and not the other.

A video is the same pipeline with more than one frame: each frame is scored independently and
decided independently, and only then are the per-frame verdicts collapsed by
tagverify/analyze/aggregate.py. Nothing between here and the model knows the difference.

WHICH SCORER ANSWERS IS A CONFIG FLAG, AND THIS FILE IS WHERE IT IS READ.
Exactly two things differ between them, and they are `_scorer_health` and `_score` below: what
can be scored, and the raw rows. Everything else — the unknown-tag gate, the result cache, the
audit row, the collapse and the response shape — is written once and runs identically either
way. That is deliberate: the two have to be comparable on the same box against the same eval
set, and a rollback has to be an environment variable rather than a release.

How frames reach the scorer differs and does not matter here. SigLIP takes one still per call,
because the Space is a single queued process; a VLM takes all of them in one request, because
a round trip is the expensive part. Both return a verdict PER FRAME, which is what keeps
aggregate.py the only thing that collapses them (AGENTS.md rule 10).

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

from tagverify.analyze.aggregate import aggregate
from tagverify.analyze.intake import Intake
from tagverify.db.cache import find_cached, record_analysis
from tagverify.scoring import registry
from tagverify.scoring.base import InferenceError, ScorerHealth
from tagverify.tags.decide import (
    Thresholds,
    Verdict,
    cached_thresholds,
    decide_all,
    decision_version,
)

log = logging.getLogger(__name__)



def _scorer() -> str:
    return registry.name()


async def _scorer_health(session: AsyncSession) -> ScorerHealth:
    """
    Seam one: what can be scored, and the fingerprint of the question.

    Under SigLIP all three answers come off the Space's /health, because the phrase pack lives
    in that process's memory and nothing else can see it. Under a VLM the catalog IS the
    question, so they are a database read and a hash — which is why saving a tag makes it live
    and there is no publish step to forget.

    Everything downstream reads the same three fields either way, so the unknown-tag gate
    below, the cache key, the audit row and the staleness check are untouched by the swap.
    """
    scorer = registry.module()
    if scorer is None:
        raise InferenceError(
            f"SCORER={registry.name()!r} is not a scorer this build knows. "
            f"Valid values: {', '.join(registry.KNOWN)}."
        )
    return await scorer.health(session)


def _scorer_rule() -> str:
    """The active scorer's half of `decision_version`. See scoring/registry.rule()."""
    return registry.rule()


async def _score(intake: Intake, health: ScorerHealth) -> list[dict[str, Any]]:
    """
    Seam two: raw per-tag rows, one set per frame.

    Both branches return the same list-of-dicts, carrying `frame_index` / `timestamp_s` for
    video only — so `aggregate.py` remains the sole owner of the collapse, the result cache
    re-aggregates a stored video correctly, and neither knows which scorer produced the rows.
    """
    scorer = registry.module()
    if scorer is None:
        raise InferenceError(f"SCORER={registry.name()!r} is not a scorer this build knows.")
    return await scorer.score_frames(intake, health)


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
    #: Never returned by the public API — see tagverify/api/v1/tags.py for why.
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
    health = await _scorer_health(session)

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
        raw = await _score(intake, health)

    # Decide EVERY frame, then collapse. Deciding first is what lets the sigmoid floor veto a
    # frame before it can win on score alone; see tagverify/analyze/aggregate.py.
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
            decision_version=decision_version(thresholds, _scorer_rule()),
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


async def write_audit(audit: dict[str, Any]) -> None:
    """Background-task entrypoint for the audit row produced by `run_analysis`."""
    await record_analysis(**audit)
