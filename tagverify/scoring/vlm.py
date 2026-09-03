"""
The ONLY module that knows a vision language model is answering.

It was written as the counterpart to a `scoring/client.py` that knew about the Gradio Space;
that module is gone with the ranking model, and this containment is what survived it.
Everything else calls `health()` and `score_frames()` and gets the same raw rows back from any
scorer -- which is what kept `analyze/run.py`, `analyze/aggregate.py`, the result cache, the
audit row and the public response shape identical across the swap, and what keeps `gemini.py`
and `fake.py` interchangeable with this one now.

WHY THE ANSWER IS DIFFERENT IN KIND
-----------------------------------
SigLIP ranks an image against a pool of phrases and returns a similarity. It cannot answer
"is there alcohol here?", only "which of these phrases fits best", which is why every tag
needs positives, hard negatives and a calibrated threshold before it means anything.

A VLM reads the image and answers the question. So `present` arrives decided, `null` is a
first-class answer the model can give rather than a band between two cutoffs, and there is
nothing to calibrate. `tags/decide.py` therefore branches on `decided_by == "vlm"` and skips
banding entirely -- see the note there.

The prompt and the schema live in `scoring/prompt.py`, deliberately separate: they are the
artefact the accuracy of this whole path rests on, and they are fingerprinted into
`decision_version` so that editing them re-keys cached verdicts.

WHAT IT RAISES, AND WHY THOSE TYPES
-----------------------------------
`InferenceWarming` and `InferenceError`, imported from `scoring/base.py`. They are not
Gradio concepts -- they mean "retry, this is expected" and "the scorer failed" -- and every
caller in the codebase already translates them into the right HTTP status, playground message
and admin banner. Reusing them means the API tier needs no change at all.

Nothing here ever returns a partial or optimistic result. A refusal, a timeout, a malformed
reply or a short frame list all raise, and a raised error becomes a FLAG downstream, never a
pass. AGENTS.md rules 1 and 11.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import json
import logging
import time
from typing import Any

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from tagverify.analyze.intake import Intake
from tagverify.config import settings
from tagverify.scoring import prompt as prompt_module
from tagverify.scoring.base import InferenceError, InferenceWarming, ScorerHealth
from tagverify.tags.catalog import list_tags

log = logging.getLogger(__name__)


# -------------------------------------------------------------------- plumbing

#: THE DEFAULT, not the only value -- `resolve_deadline` below lets a caller pass its own.
#:
#: SIZED AGAINST THE MODEL, NOT AGAINST THE CALLER. These used to be 12s/28s, justified by
#: dooh-backend abandoning a still at 7s (BATCH_DEADLINE_MS) and 10s (ANALYZE_TIMEOUT_MS) --
#: "a longer wait here is time nobody is waiting for". That reasoning inverted the dependency
#: and the result was a gate that never gated: measured latency for a 24-tag still on this
#: catalog ran 8.0s to 37.0s (median ~13s), so EVERY uncached upload analysis was abandoned by
#: one side or the other, dooh-backend recorded NOT_VERIFIED, and its policy turned that into
#: "flag for review" -- an upload that was never checked being accepted onto a screen that
#: blocks the very tag the creative contains.
#:
#: So the ceiling now sits above the model's own spread and dooh-backend's budgets were raised
#: to match (PER_IMAGE_BUDGET_MS / ANALYZE_TIMEOUT_MS there). A deadline shorter than the thing
#: it is waiting for is not a budget, it is a guaranteed timeout.
#:
#: Video is six frames in one request and gets proportionally more.
#: (An earlier comment here claimed both sat under a `TOTAL_BUDGET_S` in analyze/run.py. No
#: such constant has ever existed; there is no overall wall-clock budget.)
IMAGE_TIMEOUT_S = 40.0
VIDEO_TIMEOUT_S = 60.0

#: What the playground asks for. A person is watching this one and will wait, and nothing
#: upstream aborts. Measured latency on a free-tier Gemini key ran 6.9s to 41.2s for an
#: IDENTICAL request, which is the spread this has to sit above -- see docs/VLM_SCORING.md 5.4.
#: It stays a separate number from IMAGE_TIMEOUT_S even though the two are now close: they
#: answer to different people, and the API's is the one that moves when a caller's budget does.
INTERACTIVE_TIMEOUT_S = 45.0


def resolve_deadline(kind: str, deadline_s: float | None) -> float:
    """
    Seconds this scoring call gets, given the media kind and what the caller asked for.

    ONE function, used by both providers, because the two were already copying the same
    conditional (`vlm.py` and `gemini.py` each had their own) and a deadline that differs by
    provider is a difference nobody intends. `None` means "use the API-shaped default", which
    is what every caller got before this parameter existed.
    """
    if deadline_s is not None and deadline_s > 0:
        return float(deadline_s)
    return VIDEO_TIMEOUT_S if kind == "video" else IMAGE_TIMEOUT_S


#: Exception TYPE NAMES that mean "the deadline expired", matched by name rather than by
#: import. httpx is a transitive dependency of both SDKs and asyncio's TimeoutError is
#: builtins.TimeoutError on 3.11+, so a name check covers every provider without this module
#: importing either SDK's transport layer. `is_timeout` also walks `__cause__`, because
#: google-genai wraps the httpx error before it reaches us.
_TIMEOUT_NAMES = frozenset(
    {
        "ReadTimeout",
        "ConnectTimeout",
        "WriteTimeout",
        "PoolTimeout",
        "TimeoutException",
        "TimeoutError",
        "APITimeoutError",
        "DeadlineExceeded",
    }
)


def is_timeout(exc: BaseException) -> bool:
    """True if `exc`, or anything it was raised from, is a deadline expiry."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if type(current).__name__ in _TIMEOUT_NAMES:
            return True
        current = current.__cause__ or current.__context__
    return False


def timeout_message(deadline: float) -> str:
    """
    What the user reads when the model did not answer in time.

    Written out because the obvious version is unreadable: `httpx.ReadTimeout` stringifies to
    the EMPTY STRING, so the catch-all below rendered "the model call failed (ReadTimeout): "
    -- a bare trailing colon, naming the transport class and neither the budget nor anything
    the reader can act on.
    """
    return (
        f"the model did not answer within {deadline:.0f}s. This is a provider latency "
        "problem, not a problem with the creative -- try again"
    )


#: A cap, not a target -- raising it costs nothing when the reply is short. Six frames times
#: twenty tags of JSON is already ~5k tokens before any thinking, and a truncated reply is a
#: failed request rather than a wrong one, so there is no reason to run it close.
MAX_TOKENS = 16000

#: Classification, not reasoning. Adaptive thinking stays ON -- disabling it on Opus 5 has
#: documented failure modes and this is a judgement call about an image, not a lookup -- but
#: it runs at the shallowest depth. Raise this only with a measurement in hand.
EFFORT = "low"

#: What `Verdict.decided_by` carries for this path. A MECHANISM, not a vendor: the model id
#: already travels in the fingerprint and in `model`, and putting a vendor here would churn
#: every stored verdict the day the provider changes.
DECIDED_BY = "vlm"

_client: Any = None


def _magic(data: bytes) -> str | None:
    """
    The media type of an encoded image, from its bytes.

    Sniffed, never assumed. `analyze/intake.py` returns the ORIGINAL bytes untouched when an
    image is already within the size limit, so a small PNG, WebP or GIF reaches us in its own
    encoding and only re-encoded video frames are reliably JPEG. Hardcoding image/jpeg here
    would send a mislabelled payload for exactly the small creatives most likely to be a logo
    or a flat-colour banner.

    This mirrors AGENTS.md rule 12 one level down: the kind comes from the bytes.
    """
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def client() -> Any:
    """
    The Anthropic client, built once.

    Deliberately not built at import: `config.py` promises that nothing raises on a missing
    value, so a checkout with no key still boots and reports the problem through
    `/api/v1/health` rather than refusing to start. Monitoring you cannot reach is not
    monitoring.
    """
    global _client
    if _client is not None:
        return _client

    key = (settings().anthropic_api_key or "").strip()
    if not key:
        raise InferenceError(
            "SCORER=vlm but ANTHROPIC_API_KEY is not set. Set it, set VLM_PROVIDER=gemini\n"
            "with GEMINI_API_KEY, or set SCORER=fake."
        )

    try:
        from anthropic import AsyncAnthropic
    except ImportError as exc:  # pragma: no cover - packaging failure, not a runtime path
        raise InferenceError(f"the anthropic package is not installed: {exc}") from exc

    _client = AsyncAnthropic(api_key=key, max_retries=1)
    return _client


def reset_client() -> None:
    """Drop the cached client. Used by tests, and after a config change in development."""
    global _client
    _client = None


# --------------------------------------------------------------------- schemas


class _TagAnswer(BaseModel):
    """
    One tag's answer for one frame, as the model returned it.

    Parsed rather than trusted. `output_config.format` constrains the reply to the schema in
    `prompt.py`, but validating it again here means a provider that loosens its guarantees
    shows up as a failed request rather than as a KeyError halfway through decide().
    """

    slug: str
    #: true | false | null. Pydantic keeps the three states distinct; a missing key is a
    #: validation error rather than a silent None, which is the whole point -- "the model
    #: said it could not tell" and "the model did not answer" must not look alike.
    present: bool | None
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: str


class _FrameAnswer(BaseModel):
    index: int
    tags: list[_TagAnswer]


class _Reply(BaseModel):
    frames: list[_FrameAnswer]


# ---------------------------------------------------------------------- health


async def health(session: AsyncSession) -> ScorerHealth:
    """
    What this scorer can answer, and the fingerprint of the question it will ask.

    The SigLIP equivalent is an HTTP call to the Space, because the phrase pack lives inside
    the Space's memory and Postgres cannot see it. Here the catalog IS the question, so this
    is a database read and a hash -- no network, and nothing that can be stale.

    That difference is the entire point of the change: a tag saved a second ago is in this
    list, so there is no publish step, no `packs_version` to push, and no way for a restart to
    silently revert the catalog.
    """
    rows = await list_tags(session, include_retired=False)
    specs = prompt_module.catalog_specs(rows)

    model = settings().vlm_model
    return ScorerHealth(
        tags=sorted(specs),
        packs_version=prompt_module.catalog_fingerprint(model, specs),
        model=model,
        specs=specs,
    )


# --------------------------------------------------------------------- scoring


def _image_block(frame_base64: str, index: int) -> list[dict[str, Any]]:
    """A labelled image, as two content blocks: the label, then the picture."""
    try:
        raw = base64.b64decode(frame_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InferenceError(f"frame {index} is not valid base64: {exc}") from exc

    media_type = _magic(raw)
    if media_type is None:
        raise InferenceError(f"frame {index} is not a recognised image format")

    return [
        {"type": "text", "text": f"FRAME {index}:"},
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": frame_base64,
            },
        },
    ]


def _rows(reply: _Reply, intake: Intake, wanted: set[str]) -> list[dict[str, Any]]:
    """
    Turn the model's answer into the raw rows the rest of the pipeline already understands.

    EVERY FRAME, EVERY TAG, OR THE WHOLE REQUEST FAILS.
    AGENTS.md rule 11. Verdicts computed over four frames of six, with nothing in the response
    saying so, is "we didn't check" presented as "we checked and it's clean" -- the single
    failure mode this product exists to prevent. The same applies within a frame: a missing
    tag is not an absent tag.
    """
    by_index = {frame.index: frame for frame in reply.frames}
    rows: list[dict[str, Any]] = []

    for frame in intake.frames:
        answer = by_index.get(frame.index)
        if answer is None:
            raise InferenceError(
                f"the model returned no answer for frame {frame.index} of "
                f"{len(intake.frames)}"
            )

        seen = {tag.slug for tag in answer.tags}
        if seen != wanted:
            missing = sorted(wanted - seen)
            extra = sorted(seen - wanted)
            raise InferenceError(
                f"frame {frame.index} answered the wrong tags "
                f"(missing: {missing or 'none'}, unexpected: {extra or 'none'})"
            )

        for tag in answer.tags:
            row: dict[str, Any] = {
                "tag": tag.slug,
                # Decided by the model, not by a threshold. `decide()` reads this directly.
                "present": tag.present,
                # The model's own certainty, occupying the `score` slot. A DIFFERENT MEANING
                # from SigLIP's similarity -- self-reported, not measured -- which is why it
                # is display-only and nothing compares it against a cutoff.
                "score": tag.confidence,
                "top_phrase": tag.evidence,
                "decided_by": DECIDED_BY,
            }
            # Only for video, exactly as the SigLIP path does it, so a cache entry written
            # before this change still decodes cleanly and aggregate.py needs no special case.
            if intake.kind == "video":
                row["frame_index"] = frame.index
                row["timestamp_s"] = frame.timestamp_s
            rows.append(row)

    return rows


async def score_frames(
    intake: Intake,
    scorer_health: ScorerHealth,
    deadline_s: float | None = None,
) -> list[dict[str, Any]]:
    """
    Score every frame of a creative in ONE request.

    One call rather than one per frame, because a hosted model is a network round trip rather
    than a queued local process: six sequential calls would spend six times the latency for
    nothing. This is a TRANSPORT decision and the schema keeps it from becoming a
    decision-making one -- the reply carries a verdict per frame per tag, and
    `analyze/aggregate.py` remains the only thing that collapses them.

    The prompt asks for per-frame independence explicitly, because the frames do share one
    context. See scoring/prompt.py for which direction of leakage is harmless and which one is
    a missed detection.
    """
    specs = {slug: scorer_health.specs[slug] for slug in intake.tags}
    wanted = set(specs)

    content: list[dict[str, Any]] = []
    for frame in intake.frames:
        content.extend(_image_block(frame.base64, frame.index))
    content.append(
        {"type": "text", "text": prompt_module.render_question(specs, len(intake.frames))}
    )

    timeout = resolve_deadline(intake.kind, deadline_s)

    started = time.monotonic()
    try:
        response = await client().with_options(timeout=timeout).messages.create(
            model=scorer_health.model,
            max_tokens=MAX_TOKENS,
            system=prompt_module.SYSTEM,
            messages=[{"role": "user", "content": content}],
            thinking={"type": "adaptive"},
            output_config={
                "effort": EFFORT,
                "format": {"type": "json_schema", "schema": prompt_module.SCHEMA},
            },
        )
    except Exception as exc:  # noqa: BLE001 - re-raised as our own types below
        # LOG BEFORE RE-RAISING. Every caller of this function turns the exception into a
        # message for a human -- and the playground turns it into an HTTP *200* carrying an
        # error fragment, so without this line a failed analysis left no server-side trace at
        # all beyond uvicorn's `200 OK`. A failure rate nobody can see is a failure rate
        # nobody manages.
        log.warning(
            "vlm call failed after %.1fs (deadline %.0fs) model=%s frames=%d tags=%d: %s: %s",
            time.monotonic() - started,
            timeout,
            scorer_health.model,
            len(intake.frames),
            len(wanted),
            type(exc).__name__,
            exc,
        )
        raise _translate(exc, timeout) from exc

    # Billed from the first call, not from the first surprise. docs/VLM_SCORING.md 7 is an
    # estimate; this is what makes the monthly figure a measurement.
    usage = getattr(response, "usage", None)
    log.info(
        "vlm scored %s frame(s) x %d tag(s) model=%s in=%s out=%s",
        len(intake.frames),
        len(wanted),
        scorer_health.model,
        getattr(usage, "input_tokens", "?"),
        getattr(usage, "output_tokens", "?"),
    )

    # A safety refusal is NOT an absent verdict. Failing here means the creative is flagged
    # for a human, which is the correct outcome; returning "nothing found" would be the bug.
    if getattr(response, "stop_reason", None) == "refusal":
        raise InferenceError("the model declined to answer; the creative needs a human")

    text = next(
        (block.text for block in response.content if getattr(block, "type", "") == "text"),
        None,
    )
    if not text:
        raise InferenceError("the model returned no text block")

    try:
        reply = _Reply.model_validate(json.loads(text))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise InferenceError(f"unexpected response from the model: {exc}") from exc

    if len(reply.frames) != len(intake.frames):
        raise InferenceError(
            f"the model answered {len(reply.frames)} frame(s), {len(intake.frames)} were sent"
        )

    return _rows(reply, intake, wanted)


def _translate(exc: Exception, deadline: float | None = None) -> Exception:
    """
    Map an SDK failure onto the two error types the rest of the codebase already handles.

    The split is retryable versus not, and it decides an HTTP status a caller acts on:

    - `InferenceWarming` -> 503 with `Retry-After`. Only rate limiting earns this. It is a
      queueing problem with a known recovery, exactly like a Space still loading its weights.
    - `InferenceError` -> 502. Everything else, INCLUDING timeouts. A timeout against a hosted
      API is not a warm-up, and telling a caller to retry something that will time out again
      just moves the failure. dooh-backend turns a 502 into a review flag, which is the
      honest outcome.

    A TIMEOUT STAYS AN `InferenceError`, and that is deliberate. It now carries a message that
    names the budget instead of a bare `ReadTimeout`, but the TYPE does not move: promoting it
    to `InferenceWarming` would turn the public API's 502 into a 503 with `Retry-After`, and
    dooh-backend reads that difference. The playground surfaces its own Retry button off
    `is_timeout` instead -- a UI affordance, not a contract change.

    Never a clean pass, in any branch.
    """
    name = type(exc).__name__

    if name == "RateLimitError":
        retry_after = 30
        response = getattr(exc, "response", None)
        if response is not None:
            with contextlib.suppress(AttributeError, TypeError, ValueError):
                retry_after = int(response.headers.get("retry-after", "30"))
        return InferenceWarming(f"rate limited by the model provider: {exc}", retry_after)

    if isinstance(exc, (InferenceError, InferenceWarming)):
        return exc

    if is_timeout(exc):
        return InferenceError(timeout_message(deadline or IMAGE_TIMEOUT_S))

    return InferenceError(f"the model call failed ({name}): {exc}")
