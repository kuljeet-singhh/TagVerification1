"""
Turn one raw row into a verdict.

THE RULE USED TO LIVE SOMEWHERE ELSE, AND USED TO BE MUCH BIGGER
----------------------------------------------------------------
It was `inference/banding.py`: two cutoffs and an absolute-resemblance floor applied to a
similarity score, shared with the SigLIP tier because that tier had to speak it too. All of
that existed because a similarity means nothing on its own -- measured true positives ran
from 0.028 to 0.902 -- so every tag needed calibrating before any verdict meant anything.

A reading model answers the question instead of scoring a resemblance to it, so `present`
arrives decided and there is nothing left to band. What remains here is the tri-state
passthrough, three confidence cutoffs for triage, and the staleness check that keeps
`calibrated` honest.

`decide()` is pure -- no database, no network -- so it can be tested exhaustively. Loading
thresholds is a separate concern, below.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import tagverify.analyze.video
from tagverify.db.models import TagThreshold

#: The three verdict vocabularies. They lived in inference/banding.py, which was shared with
#: the SigLIP tier because that tier had to speak them too. Nothing else does now, and they
#: are three Literals with no logic attached, so they come home to the module that owns the
#: decision rather than being imported across a tier boundary that no longer exists.
Band = Literal["present", "absent", "uncertain"]
Confidence = Literal["high", "medium", "low"]
#: A MECHANISM, not a vendor. "siglip" and "sigmoid_floor" are gone with the ranking model;
#: the union stays open (`| str`) so a stored verdict written by either of them still parses.
DecidedBy = Literal["vlm"]

#: Fingerprint of the decision rule this process loaded, read ONCE AT IMPORT. Deliberately.
#:
#: It hashed inference/banding.py, because that file WAS the rule: a score, two cutoffs and a
#: floor. With the ranking model gone there is no score to band, and the rule is this module —
#: the tri-state passthrough and the confidence cutoffs below. So it hashes this file.
#:
#: Read once, not per request. It has to describe the rule THIS PROCESS IS RUNNING, not the
#: file currently on disk: re-reading would make a server started without `--reload` advertise
#: a fingerprint for code it is not executing, which answers "is my change live?" with a
#: confident yes. A stale process keeps its old fingerprint, which is exactly the signal
#: wanted.
RULE_VERSION = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:12]

#: Fingerprint of the FRAME SAMPLER this process loaded. Read once at import, same as above.
#:
#: WHY A VERDICT DEPENDS ON THIS
#: -----------------------------
#: For a video, which frames get sampled decides which pixels are ever scored — so it changes
#: the verdict as surely as a threshold does. Neither existing fingerprint covered it, and the
#: gap was not academic. A sampler bug judged an eight-second creative on its opening frame and
#: passed a blocked one; the fix could not reach that creative, because the wrong verdict was
#: cached under a (packs_version, decision_version) pair that a sampler change did not move.
#:
#: It is folded in HERE rather than into packs_version because of what each one covers.
#: packs_version covers the QUESTION -- the model id, the prompt and the catalog -- and frame
#: sampling is not part of the question; it is part of how this tier decides what to ask about.
#: (Under SigLIP the separation was physical too: the fingerprint was stamped inside the Space,
#: which received one still and could not see analyze/video.py.) Cost of the placement:
#: bumping the sampler re-keys
#: cached image verdicts too, which is wasteful but never wrong.
SAMPLER_VERSION = hashlib.sha256(
    Path(tagverify.analyze.video.__file__).read_bytes()
).hexdigest()[:12]


@dataclass(slots=True)
class FrameRef:
    """Which sampled frame a video verdict came from."""

    index: int
    timestamp_s: float


@dataclass(slots=True)
class Evidence:
    top_phrase: str
    #: ALWAYS None now. The model reports what it SAW rather than which of ten crops best
    #: matched which canned phrase, and there is no other scorer left to fill them.
    #:
    #: Kept rather than removed for two reasons that outlived the ranking model: stored
    #: `analyses` rows carry real values in both, and dooh-backend already parses them
    #: (content-verification/client.ts guards `crop` with Array.isArray and passes `sigmoid`
    #: through a numeric coercion). Dropping them from the response is a breaking change to
    #: buy nothing. tests/test_vlm.py pins that they are null and never 0 -- a zero would read
    #: as a measured value.
    crop: list[float] | None
    sigmoid: float | None
    #: None for a still. Set for video, and it is the WHOLE point of the video path: "contains
    #: alcohol" is not actionable, "contains alcohol at 4.2s, in this crop" is.
    frame: FrameRef | None = None


#: Left open (`| str`) rather than narrowed to DecidedBy: `creative_tag_analyses` still holds
#: verdicts written by "siglip" and "sigmoid_floor", and a stored row must keep parsing after
#: the model that wrote it is gone. Retire, never delete, applies to values too.
VerdictSource = DecidedBy | str


@dataclass(slots=True)
class Verdict:
    tag: str
    #: None = uncertain; the caller should escalate to a vision LLM or a human.
    present: bool | None
    score: float
    confidence: Confidence
    decided_by: VerdictSource
    #: False when this tag's thresholds have never been calibrated against labelled data.
    #: Surfaced to callers on purpose — an uncalibrated verdict is a guess and should not be
    #: presented as a measurement.
    calibrated: bool
    evidence: Evidence
    band: Band = field(default="absent")

    def to_api(self) -> dict[str, Any]:
        """The public API shape. Keys are snake_case and must not change."""
        evidence: dict[str, Any] = {
            "top_phrase": self.evidence.top_phrase,
            "crop": self.evidence.crop,
            "sigmoid": self.evidence.sigmoid,
        }
        # Added only when there is one, so an image response stays byte-identical to what
        # every existing integration already parses. Its presence is the signal that this
        # verdict came from a video.
        if self.evidence.frame is not None:
            evidence["frame"] = {
                "index": self.evidence.frame.index,
                "timestamp_s": self.evidence.frame.timestamp_s,
            }

        return {
            "tag": self.tag,
            "present": self.present,
            "score": self.score,
            "confidence": self.confidence,
            "decided_by": self.decided_by,
            "calibrated": self.calibrated,
            "evidence": evidence,
        }


@dataclass(slots=True)
class Thresholds:
    """
    The subset of a tag_thresholds row that bears on a verdict.

    NOT A THRESHOLD ANY MORE, despite the name and the table it comes from. `threshold_low`,
    `threshold_high` and `sigmoid_floor` were cutoffs applied to a similarity; migration 0004
    dropped them, because `decide()` had stopped reading them when the ranking model went and
    a number nothing reads is a number that goes wrong quietly. The name is kept because the
    table is called `tag_thresholds` and renaming both is churn for no reader.

    What is left is the answer to "has this tag ever been measured, and does that measurement
    still describe the prompts we are serving?" -- AGENTS.md rule 4, and nothing else.
    """

    calibrated: bool = False
    #: The `packs_version` these numbers were measured against. A different live fingerprint
    #: makes the measurement stale -- see `effective_calibrated`.
    packs_version_seen: str | None = None
    precision: float | None = None
    recall: float | None = None


#: Used when a tag has no `tag_thresholds` row at all: never measured, which is what the
#: default `calibrated=False` says.
FALLBACK = Thresholds()


def effective_calibrated(t: Thresholds, live_packs_version: str | None) -> bool:
    """
    Is this tag's calibration BOTH present and still current?

    A tag measured against a different catalog is stale, not calibrated: the prompts were
    reworded underneath the measurement, so it no longer describes the answers being produced.
    Rule 4 again -- presenting a superseded measurement as a current one is the same failure as
    presenting a guess as a measurement.

    ONE FUNCTION BECAUSE THERE USED TO BE TWO, AND THEY DRIFTED. This comparison lived inline
    in `decide()` and again in `api/v1/tags.py`, and the admin panel had a third answer: it
    read `row.calibrated` raw and rendered eight tags as measured while every API response
    said none of them were. A reader on that page saw P 69% R 93% for a calibration that had
    been stale since the prompts changed. Three copies of a rule is how a compliance tool ends
    up disagreeing with itself; there is one now, and every surface calls it.

    `live_packs_version` of None means "we could not ask" -- then the stored flag is the best
    available answer and is passed through, rather than manufacturing a `false` out of our own
    ignorance.
    """
    if not t.calibrated:
        return False
    if not live_packs_version or not t.packs_version_seen:
        return True
    return t.packs_version_seen == live_packs_version


def decide(
    raw: dict[str, Any],
    threshold: Thresholds | None,
    live_packs_version: str | None = None,
) -> Verdict:
    """
    Turn one raw row into a verdict.

    THERE IS NO BANDING LEFT. SigLIP returned a similarity, and a similarity means nothing on
    its own — measured true positives ran from 0.028 to 0.902 — so a verdict was two cutoffs
    and an absolute-resemblance floor applied to it, and every tag needed calibrating before
    any of it meant anything. A reading model answers the question, so `present` arrives
    decided and this function's job is to carry it out unchanged.

    That is the whole shape of the change, and it is why `calibrated` survives while the
    thresholds do not: there is nothing left to tune, but "has this tag ever been measured
    against labelled data?" is still a question a caller must be able to ask (AGENTS.md
    rule 4).

    `frame_index` / `timestamp_s` are attached by the video path before this runs. Each frame
    is decided INDEPENDENTLY here and only then collapsed — see analyze/aggregate.py for why
    that order matters.
    """
    t = threshold or FALLBACK

    present = raw["present"]
    if present is not None and not isinstance(present, bool):
        # Belt and braces behind the schema. A stringy "true" is not a decision, and treating
        # it as one is how a wrong verdict gets served with full confidence.
        raise ValueError(f"{raw.get('tag')!r}: present must be a bool or None, got {present!r}")

    score = float(raw["score"])

    if present is None:
        # An uncertain verdict has no margin to measure.
        confidence: Confidence = "low"
    elif score >= VLM_HIGH:
        confidence = "high"
    elif score >= VLM_MEDIUM:
        confidence = "medium"
    else:
        confidence = "low"

    return Verdict(
        tag=raw["tag"],
        present=present,
        score=score,
        confidence=confidence,
        decided_by="vlm",
        calibrated=effective_calibrated(t, live_packs_version),
        band="present" if present else ("absent" if present is False else "uncertain"),
        evidence=Evidence(
            top_phrase=raw["top_phrase"],
            # No crop and no sigmoid to report. Null, not zero: a zero would read as a
            # measured absence of resemblance rather than a question never asked.
            crop=None,
            sigmoid=None,
            frame=(
                FrameRef(int(raw["frame_index"]), float(raw["timestamp_s"]))
                if "frame_index" in raw
                else None
            ),
        ),
    )


#: Confidence bands for a self-reported certainty. Not the SigLIP bands, and not comparable to
#: them: `confidence_of` measures MARGIN past a calibrated cutoff, which is meaningless when
#: there is no cutoff. These read the model's own 0-1 certainty directly.
#:
#: Triage only. Nothing enforces on `confidence` -- dooh-backend branches on `present` alone --
#: so these numbers cannot block or clear a creative. They exist so a reviewer can sort the
#: queue by "answered, but barely".
VLM_HIGH = 0.85
VLM_MEDIUM = 0.60


def decide_all(
    results: list[dict[str, Any]],
    thresholds: dict[str, Thresholds],
    live_packs_version: str | None = None,
) -> list[Verdict]:
    return [decide(raw, thresholds.get(raw["tag"]), live_packs_version) for raw in results]


# --------------------------------------------------------------------- loading


def _to_thresholds(row: TagThreshold) -> Thresholds:
    return Thresholds(
        calibrated=row.calibrated,
        packs_version_seen=row.packs_version_seen,
        precision=row.precision,
        recall=row.recall,
    )


async def load_thresholds(session: AsyncSession) -> dict[str, Thresholds]:
    rows = (await session.execute(select(TagThreshold))).scalars().all()
    return {row.slug: _to_thresholds(row) for row in rows}


# Memoised calibration state for the /analyze hot path.
#
# NOTHING WRITES THIS TABLE ANY MORE. The hand-edit form went with the thresholds in migration
# 0004, and `dooh apply-calibration` went with the ranking model, so the 30s TTL is now the lag
# after a write that no code path performs. It is kept rather than raised or removed because
# `docs/VLM_SCORING.md` §5.2 has the eval harness writing `calibrated` / `precision` / `recall`
# back here, and a memo that is already correct is one less thing for that change to get wrong.
#
# `invalidate_threshold_cache()` was deleted with its only caller. Restore it alongside that
# writer rather than leaving a function nothing calls.
_TTL_SECONDS = 30.0
_cache: tuple[float, dict[str, Thresholds]] | None = None


async def cached_thresholds(session: AsyncSession) -> dict[str, Thresholds]:
    global _cache
    if _cache is not None and (time.monotonic() - _cache[0]) < _TTL_SECONDS:
        return _cache[1]
    value = await load_thresholds(session)
    _cache = (time.monotonic(), value)
    return value


def decision_version(
    thresholds: dict[str, Thresholds],
    scorer_rule: str = "",
) -> str:
    """
    Fingerprint of everything that turns a raw score into a verdict.

    WHAT THIS IS FOR
    ----------------
    `packs_version` fingerprints the PROMPTS, so a caller can tell when the question we ask has
    changed. Nothing fingerprinted the other half — the rule the answer is read by — and that
    gap is not academic.

    A downstream integrator caching our verdicts keyed on `packs_version` alone keeps serving
    a refusal we have since decided is wrong, and never calls us again to find out. That is
    not hypothetical: this service re-decides from raw answers on every read precisely so a
    change to the rule applies retroactively, and a consumer cache silently undid it. Handing
    out this value lets them put it in their key and get the retroactivity back.

    It is also the answer to "is my change live?", which is otherwise unanswerable from
    outside the process — see RULE_VERSION.

    WHAT IT COVERS
    --------------
    The rule (this module, via RULE_VERSION) and every field of every `Thresholds`, INCLUDING
    the fallback used for a tag with no row at all (`decide()` does `t = threshold or FALLBACK`).
    Adding, removing or editing a row all move it.

    There are no cutoffs left to cover -- migration 0004 dropped them -- so what folds in now
    is the calibration state: `calibrated`, `packs_version_seen`, `precision`, `recall`. That
    is NOT over-covering by accident. `calibrated` is served to callers on every verdict and
    `packs_version_seen` decides it (see `effective_calibrated`), so a caller keying its cache
    on this value gets a re-key when the honesty of a verdict changes, which is exactly when
    it should stop serving the old one.

    Every field is folded in MECHANICALLY rather than hand-picking the ones that currently
    affect a verdict. A hand-picked tuple silently under-covers the moment somebody adds a
    field and wires it into `decide()` — which is exactly the class of bug this exists to
    prevent, and the reason this survived the thresholds it was written for.

    It deliberately does NOT cover the prompts. That is `packs_version`, and folding the two
    together would make each one's meaning unreadable.

    It DOES cover the frame sampler (`SAMPLER_VERSION`), because for a video the choice of
    frames decides which pixels are scored at all — a verdict input that neither fingerprint
    used to describe. See that constant for why it lives here and not with the prompts.

    `scorer_rule` IS THE VLM's HALF OF THE SAME IDEA. Under SigLIP the whole rule was
    `banding.py` plus the cutoffs. There are no cutoffs, and the rule is the prompt template,
    the response schema and the confidence bands above -- so the caller passes a fingerprint of
    those and it folds in here. Same contract, different inputs.

    IT APPENDS ONLY WHEN NON-EMPTY, and that is load-bearing rather than tidy. An empty
    `scorer_rule` has to produce the byte-identical digest this function returned before the
    argument existed, or simply ADDING the VLM path -- switched off, behind a flag nobody has
    flipped -- would re-key every cached verdict in this service and in dooh-backend. A cache
    invalidation is cheap; one triggered by a change that cannot affect a single verdict is
    just noise, and noise is how a real re-key later gets ignored.

    NOT A LEAK. This is a one-way hash, truncated to 48 bits. AGENTS.md rule 7 forbids exposing
    thresholds through the API, and a digest is not the thing: you cannot read a value back out,
    and a caller tuning a creative against it learns nothing.
    """
    seed = f"{RULE_VERSION}|{SAMPLER_VERSION}"
    if scorer_rule:
        seed += f"|{scorer_rule}"
    digest = hashlib.sha256(seed.encode())

    def fold(key: str, t: Thresholds) -> None:
        digest.update(f"|{key}".encode())
        for f in fields(t):
            value = getattr(t, f.name)
            # float.hex() is exact and unambiguous, where repr() invites a formatting change
            # to quietly re-key every cache in the fleet. None must not collide with "".
            token = value.hex() if isinstance(value, float) else f"\x01{value!r}"
            digest.update(f":{f.name}={token}".encode())

    # Sorted, so dict iteration order cannot make an unchanged config look changed.
    for slug in sorted(thresholds):
        fold(slug, thresholds[slug])
    # A key no slug can collide with — slugs are [a-z_].
    fold("\x00fallback", FALLBACK)

    return digest.hexdigest()[:12]


