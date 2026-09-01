"""
Turn a raw score into a verdict, using thresholds from Postgres.

WHY WE RE-DECIDE HERE INSTEAD OF TRUSTING THE SPACE
---------------------------------------------------
The Space carries provisional thresholds in packs.json so its own test UI can show something
useful. But a threshold is only ever a comparison against a number that has already been
computed — so applying it here, in the API tier, costs nothing and buys a lot: calibration
results and hand-tuning take effect on the next request with no Space redeploy and no model
reload.

The prompts cannot work this way (the Space must encode them into embeddings at startup),
which is exactly why prompts live in git and thresholds live in the database.

The banding rule itself is NOT written here. It is imported from inference/banding.py, the
same function the Space and the calibration sweep use. See that file for why.

`decide()` is pure — no database, no network — so it can be tested exhaustively. Loading
thresholds is a separate concern, below.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import inference.banding
import tagverify.analyze.video
from inference.banding import Band, Confidence, DecidedBy, band_of, confidence_of
from tagverify.db.models import TagThreshold

#: Fingerprint of the banding rule this process loaded, read ONCE AT IMPORT. Deliberately.
#:
#: This has to describe the rule THIS PROCESS IS RUNNING, not the file currently on disk.
#: Re-reading per request would make a server that was started without `--reload` advertise a
#: fingerprint for code it is not executing — worse than having none, because it would answer
#: "is my change live?" with a confident yes. A stale process keeps its old fingerprint, which
#: is exactly the signal wanted.
RULE_VERSION = hashlib.sha256(
    Path(inference.banding.__file__).read_bytes()
).hexdigest()[:12]

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
#: It is folded in HERE rather than into packs_version because of where each is computed.
#: packs_version is stamped by the detector inside the Space, which only ever receives a single
#: still and cannot see tagverify/analyze/video.py at all. decision_version is computed in this
#: tier, which is where frames are chosen. Cost of the placement: bumping the sampler re-keys
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
    #: Both None under a VLM, which reports what it SAW rather than which of ten crops best
    #: matched which canned phrase. Optional rather than removed, because the SigLIP path
    #: still fills them and dooh-backend already tolerates null in both
    #: (content-verification/client.ts guards `crop` with Array.isArray and passes `sigmoid`
    #: through a numeric coercion), so no consumer changes.
    crop: list[float] | None
    sigmoid: float | None
    #: None for a still. Set for video, and it is the WHOLE point of the video path: "contains
    #: alcohol" is not actionable, "contains alcohol at 4.2s, in this crop" is.
    frame: FrameRef | None = None


#: What decided a verdict. `inference/banding.py` owns the two SigLIP mechanisms and is left
#: alone on purpose: its bytes are RULE_VERSION, so adding a member there would move
#: `decision_version` for the SigLIP path too and re-key every cached verdict in this service
#: AND in dooh-backend for a scorer that is not even switched on. It is also the one file that
#: is git-subtree-pushed to the Space, which has no concept of a VLM. So the union is widened
#: here, where the extra mechanism actually exists.
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
    """The subset of a tag_thresholds row that decides a verdict."""

    threshold_low: float = 0.3
    threshold_high: float = 0.55
    sigmoid_floor: float = 0.005
    calibrated: bool = False
    packs_version_seen: str | None = None
    precision: float | None = None
    recall: float | None = None


#: Used when a tag has no row yet. Matches the defaults block in packs.json.
FALLBACK = Thresholds()


def decide(
    raw: dict[str, Any],
    threshold: Thresholds | None,
    live_packs_version: str | None = None,
) -> Verdict:
    """
    Decide one tag's verdict from the raw scores the model returned.

    `raw` is a verdict dict from the inference service. We use only `score`, `sigmoid`,
    `top_phrase` and `crop` from it — its own `present`/`band` are deliberately ignored,
    because the Space decided those with the provisional thresholds baked into packs.json
    rather than the tuned ones in Postgres.

    `frame_index` / `timestamp_s` are attached by the video path before this runs; the Space
    knows nothing about them. Each frame is decided INDEPENDENTLY here, and only then are the
    per-frame verdicts collapsed — see tagverify/analyze/aggregate.py for why that order matters.
    """
    t = threshold or FALLBACK

    # A VLM answered the question rather than scoring a resemblance to it, so there is no
    # number to band and nothing to compare against a cutoff. Branch BEFORE the threshold
    # work, not inside it: `band_of` would read a `sigmoid` that does not exist, and passing
    # it a self-reported confidence would silently reinterpret a different quantity as a
    # similarity. See tagverify/scoring/vlm.py.
    #
    # Staleness still applies below via `calibrated`, so AGENTS.md rule 4 is untouched: a tag
    # nobody has run against the eval set still reports calibrated: false.
    if raw.get("decided_by") == "vlm":
        return _decide_vlm(raw, t, live_packs_version)

    # A threshold is only meaningful for the prompts it was measured against. Editing a pack
    # changes the scores the model produces, so a threshold calibrated against an older pack
    # no longer describes anything — and this drifts silently: calibrate, apply, forget to
    # redeploy the Space, and stale cutoffs get applied to different numbers with no error
    # anywhere.
    #
    # We keep using the threshold (it is still the best guess available) but stop claiming it
    # is calibrated, so `calibrated: false` reaches the caller and the admin UI shows the tag
    # needs re-running. This was a real incident during development, not a hypothetical.
    stale = bool(live_packs_version) and bool(t.packs_version_seen) and (
        t.packs_version_seen != live_packs_version
    )
    calibrated = t.calibrated and not stale

    score = float(raw["score"])
    sigmoid = float(raw["sigmoid"])

    band, present, decided_by = band_of(
        score, sigmoid, t.threshold_low, t.threshold_high, t.sigmoid_floor
    )

    return Verdict(
        tag=raw["tag"],
        present=present,
        score=score,
        confidence=confidence_of(score, t.threshold_low, t.threshold_high, band),
        decided_by=decided_by,
        calibrated=calibrated,
        band=band,
        evidence=Evidence(
            top_phrase=raw["top_phrase"],
            crop=list(raw["crop"]),
            sigmoid=sigmoid,
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


def _decide_vlm(
    raw: dict[str, Any],
    t: Thresholds,
    live_packs_version: str | None,
) -> Verdict:
    """
    A verdict the model already reached. No banding, no floor, no thresholds.

    `present` is carried through EXACTLY as the model gave it, including None. That is the
    line AGENTS.md rule 1 draws and this path makes it native rather than derived: under
    SigLIP "uncertain" was the gap between two cutoffs, here it is an answer the model chose
    to give. Flattening it to False anywhere between here and the API would turn "we could not
    tell" into "we checked and it is clean", which is the failure this product exists to
    prevent.
    """
    present = raw["present"]
    if present is not None and not isinstance(present, bool):
        # Belt and braces behind the schema. A stringy "true" is not a decision; treating it
        # as one is how a wrong verdict gets served with full confidence.
        raise ValueError(f"{raw.get('tag')!r}: present must be a bool or None, got {present!r}")

    score = float(raw["score"])

    if present is None:
        # An uncertain verdict has no margin to measure, exactly as in confidence_of().
        confidence: Confidence = "low"
    elif score >= VLM_HIGH:
        confidence = "high"
    elif score >= VLM_MEDIUM:
        confidence = "medium"
    else:
        confidence = "low"

    stale = bool(live_packs_version) and bool(t.packs_version_seen) and (
        t.packs_version_seen != live_packs_version
    )

    return Verdict(
        tag=raw["tag"],
        present=present,
        score=score,
        confidence=confidence,
        decided_by="vlm",
        calibrated=t.calibrated and not stale,
        band="present" if present else ("absent" if present is False else "uncertain"),
        evidence=Evidence(
            top_phrase=raw["top_phrase"],
            # No crops and no sigmoid to report. Null, not zero: a zero would read as a
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


def decide_all(
    results: list[dict[str, Any]],
    thresholds: dict[str, Thresholds],
    live_packs_version: str | None = None,
) -> list[Verdict]:
    return [decide(raw, thresholds.get(raw["tag"]), live_packs_version) for raw in results]


# --------------------------------------------------------------------- loading


def _to_thresholds(row: TagThreshold) -> Thresholds:
    return Thresholds(
        threshold_low=row.threshold_low,
        threshold_high=row.threshold_high,
        sigmoid_floor=row.sigmoid_floor,
        calibrated=row.calibrated,
        packs_version_seen=row.packs_version_seen,
        precision=row.precision,
        recall=row.recall,
    )


async def load_thresholds(session: AsyncSession) -> dict[str, Thresholds]:
    rows = (await session.execute(select(TagThreshold))).scalars().all()
    return {row.slug: _to_thresholds(row) for row in rows}


# Memoised thresholds for the /analyze hot path.
#
# Thresholds change only when calibration runs or someone edits them in the admin UI — rare,
# and never mid-burst. The 30s TTL is the lag between saving a threshold and it taking
# effect, and the admin UI invalidates the cache explicitly on save so in practice the lag is
# zero for the person who made the change.
#
# This mattered far more when every query was an HTTPS round trip. It is kept because 20 rows
# that change twice a week do not need re-reading per request, not because it is load-bearing.
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
    `packs_version` fingerprints the PROMPTS, so a caller can tell when the numbers the model
    produces have changed. Nothing fingerprinted the other half — the rule those numbers are
    fed to, and the cutoffs they are compared against — and that gap is not academic.

    A downstream integrator caching our verdicts keyed on `packs_version` alone keeps serving
    a refusal we have since decided is wrong, and never calls us again to find out. That is
    not hypothetical: this service re-decides from raw scores on every read precisely so a
    threshold edit applies retroactively, and a consumer cache silently undid it. Handing out
    this value lets them put it in their key and get the retroactivity back.

    It is also the answer to "is my change live?", which is otherwise unanswerable from
    outside the process — see RULE_VERSION.

    WHAT IT COVERS
    --------------
    The rule (`inference/banding.py`) and every number a verdict is compared against, INCLUDING
    the fallback used for a tag with no row at all (`decide()` does `t = threshold or FALLBACK`,
    so editing those defaults changes verdicts for every uncalibrated tag and would otherwise
    be invisible here). Adding, removing or editing a row all move it.

    Every field of `Thresholds` is folded in mechanically rather than hand-picking the ones
    that currently affect a verdict. A hand-picked tuple silently under-covers the moment
    somebody adds a field and wires it into `decide()` — which is exactly the class of bug
    this exists to prevent. The cost of over-covering is one spare cache generation when
    calibration writes a new `precision`, and calibration rewrites the cutoffs in the same
    statement anyway.

    It deliberately does NOT cover the prompts. That is `packs_version`, and folding the two
    together would make each one's meaning unreadable.

    It DOES cover the frame sampler (`SAMPLER_VERSION`), because for a video the choice of
    frames decides which pixels are scored at all — a verdict input that neither fingerprint
    used to describe. See that constant for why it lives here and not with the prompts.

    `scorer_rule` IS THE VLM's HALF OF THE SAME IDEA. Under SigLIP the whole rule is
    `banding.py` plus the cutoffs. Under a VLM there are no cutoffs, and the rule is the prompt
    template, the response schema and the confidence bands above -- so the caller passes a
    fingerprint of those and it folds in here. Same contract, different inputs.

    IT APPENDS ONLY WHEN NON-EMPTY, and that is load-bearing rather than tidy. An empty
    `scorer_rule` has to produce the byte-identical digest this function returned before the
    argument existed, or simply ADDING the VLM path -- switched off, behind a flag nobody has
    flipped -- would re-key every cached verdict in this service and in dooh-backend. A cache
    invalidation is cheap; one triggered by a change that cannot affect a single verdict is
    just noise, and noise is how a real re-key later gets ignored.

    NOT A LEAK. This is a one-way hash, truncated to 48 bits. AGENTS.md rule 6 forbids exposing
    thresholds through the API, and a digest of them is not them: you cannot read a cutoff back
    out, and a caller tuning a creative against it learns nothing.
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


def invalidate_threshold_cache() -> None:
    """Call after writing thresholds so admin edits are visible immediately."""
    global _cache
    _cache = None
