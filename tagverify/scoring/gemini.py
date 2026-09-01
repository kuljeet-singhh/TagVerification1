"""
The Gemini implementation of the VLM scorer.

Sibling to `scoring/vlm.py`, and the reason both exist is the argument in
docs/VLM_SCORING.md 6.1: this proposal is about replacing a RANKING model with a READING one,
and which reader does the reading is a config choice. Every VLM takes its prompt per request,
which is the only property the case depends on. So the provider is one file and one setting,
and the eval harness settles the choice with numbers instead of preference.

WHAT IS SHARED, AND WHAT IS NOT
-------------------------------
Shared, and deliberately not duplicated: the prompt, the JSON schema, the raw-row shape, the
per-frame contract and the failure rules all live in `scoring/prompt.py` and are used verbatim
here. If this file and `vlm.py` ever disagree about what a verdict looks like, the comparison
the gate exists to make becomes meaningless.

Not shared: the SDK call. Google's `response_json_schema` takes standard JSON Schema, so the
same SCHEMA object goes over the wire unchanged -- including `{"type": ["boolean", "null"]}`
for `present`, which is the field that must never arrive as a string. Verified against
google-genai 2.x rather than assumed.

WHY THE MODELS API AND NOT `client.interactions`
------------------------------------------------
The newer Interactions surface is a loosely-typed passthrough (`request: Any`) whose image
shape is not documented. `client.aio.models.generate_content` is typed, has a real async
client, and its inline-image shape (`Part.from_bytes`) is stable. Guessing at an undocumented
request body for a compliance decision is not a trade worth making; revisit when the
Interactions image contract is written down.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from tagverify.analyze.intake import Intake
from tagverify.config import settings
from tagverify.scoring import prompt as prompt_module
from tagverify.scoring import vlm
from tagverify.scoring.base import ScorerHealth
from tagverify.scoring.client import InferenceError, InferenceWarming
from tagverify.tags.catalog import list_tags

log = logging.getLogger(__name__)

#: Classification, not reasoning -- the same call `vlm.EFFORT` makes for the Anthropic path.
#: Gemini 3.x replaced the old numeric `thinking_budget` with this string enum.
#:
#: LOW, not MINIMAL. The enum offers MINIMAL, but gemini-3.7-flash rejects it outright with
#: `400 INVALID_ARGUMENT: Thinking level MINIMAL is not supported for this model` -- the level
#: is per-model, and the enum advertises the union rather than what any one model accepts.
#: LOW is the lowest that every current Flash model takes. Raise it only with a measurement in
#: hand; a classifier is not a reasoning task.
THINKING_LEVEL = "LOW"

_client: Any = None


def client() -> Any:
    """
    The Gemini client, built once and never at import.

    `config.py` promises that nothing raises on a missing value, so a checkout with no key
    still boots and reports the problem through `/api/v1/health` rather than refusing to
    start.
    """
    global _client
    if _client is not None:
        return _client

    key = (settings().gemini_api_key or "").strip()
    if not key:
        raise InferenceError(
            "VLM_PROVIDER=gemini but GEMINI_API_KEY is not set. "
            "Set it, or set SCORER=siglip."
        )

    try:
        from google import genai
    except ImportError as exc:  # pragma: no cover - packaging failure, not a runtime path
        raise InferenceError(f"the google-genai package is not installed: {exc}") from exc

    _client = genai.Client(api_key=key)
    return _client


def reset_client() -> None:
    """Drop the cached client. Used by tests, and after a config change in development."""
    global _client
    _client = None


async def health(session: AsyncSession) -> ScorerHealth:
    """
    Identical in shape and meaning to `vlm.health`, differing only in the model id.

    A database read and a hash: the catalog IS the question, so a tag saved a second ago is
    already live and there is no pack to publish. `catalog_fingerprint` folds the model id in,
    so switching provider correctly invalidates verdicts decided by the other one -- two
    readers do not have to agree, and pretending they do would serve one model's answer under
    the other's name.
    """
    rows = await list_tags(session, include_retired=False)
    specs = {
        row.slug: (row.description or "").strip()
        for row in rows
        if (row.description or "").strip()
    }

    model = settings().vlm_model
    return ScorerHealth(
        tags=sorted(specs),
        packs_version=prompt_module.catalog_fingerprint(model, specs),
        model=model,
        specs=specs,
    )


def _parts(intake: Intake, specs: dict[str, str]) -> list[Any]:
    """The images, each labelled, then the question. Same order as the Anthropic path."""
    from google.genai import types

    parts: list[Any] = []
    for frame in intake.frames:
        try:
            raw = base64.b64decode(frame.base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise InferenceError(f"frame {frame.index} is not valid base64: {exc}") from exc

        # Sniffed, never assumed -- intake returns ORIGINAL bytes for an already-small image,
        # so the encoding is whatever the advertiser uploaded. See vlm._magic.
        media_type = vlm._magic(raw)
        if media_type is None:
            raise InferenceError(f"frame {frame.index} is not a recognised image format")

        parts.append(types.Part.from_text(text=f"FRAME {frame.index}:"))
        parts.append(types.Part.from_bytes(data=raw, mime_type=media_type))

    parts.append(
        types.Part.from_text(
            text=prompt_module.render_question(specs, len(intake.frames))
        )
    )
    return parts


async def score_frames(intake: Intake, scorer_health: ScorerHealth) -> list[dict[str, Any]]:
    """
    Score every frame in one request, returning the raw rows every scorer produces.

    The reply is validated by the SAME pydantic models the Anthropic path uses and turned into
    rows by the SAME function, so "every frame, every tag, or the whole request fails" is
    enforced once rather than twice. AGENTS.md rules 10 and 11.
    """
    from google.genai import types

    specs = {slug: scorer_health.specs[slug] for slug in intake.tags}
    wanted = set(specs)
    timeout = vlm.VIDEO_TIMEOUT_S if intake.kind == "video" else vlm.IMAGE_TIMEOUT_S

    config = types.GenerateContentConfig(
        system_instruction=prompt_module.SYSTEM,
        response_mime_type="application/json",
        # Standard JSON Schema, so the SAME schema object as the Anthropic path -- including
        # the boolean-or-null `present` that must never arrive as a string.
        response_json_schema=prompt_module.SCHEMA,
        max_output_tokens=vlm.MAX_TOKENS,
        thinking_config=types.ThinkingConfig(thinking_level=THINKING_LEVEL),
        # Milliseconds here, unlike the Anthropic client's seconds. An easy thing to get
        # wrong by a factor of a thousand in the direction that looks like it works.
        http_options=types.HttpOptions(timeout=int(timeout * 1000)),
    )

    try:
        response = await client().aio.models.generate_content(
            model=scorer_health.model,
            contents=[types.Content(role="user", parts=_parts(intake, specs))],
            config=config,
        )
    except Exception as exc:  # noqa: BLE001 - re-raised as our own types below
        raise _translate(exc) from exc

    usage = getattr(response, "usage_metadata", None)
    log.info(
        "gemini scored %s frame(s) x %d tag(s) model=%s in=%s out=%s",
        len(intake.frames),
        len(wanted),
        scorer_health.model,
        getattr(usage, "prompt_token_count", "?"),
        getattr(usage, "candidates_token_count", "?"),
    )

    text = getattr(response, "text", None)
    if not text:
        # Covers a safety block and an empty candidate list alike. Both mean "no answer", and
        # no answer must fail the request rather than read as nothing-found.
        reason = getattr(getattr(response, "prompt_feedback", None), "block_reason", None)
        raise InferenceError(
            f"the model returned no answer ({reason or 'empty response'}); "
            "the creative needs a human"
        )

    try:
        reply = vlm._Reply.model_validate(json.loads(text))
    except (json.JSONDecodeError, ValueError) as exc:
        raise InferenceError(f"unexpected response from the model: {exc}") from exc

    if len(reply.frames) != len(intake.frames):
        raise InferenceError(
            f"the model answered {len(reply.frames)} frame(s), {len(intake.frames)} were sent"
        )

    return vlm._rows(reply, intake, wanted)


def _translate(exc: Exception) -> Exception:
    """
    Retryable versus not, mapped onto the two types every caller already handles.

    Same split as `vlm._translate` and for the same reasons: only rate limiting earns a 503,
    and a timeout is an `InferenceError` because telling a caller to retry something that will
    time out again just moves the failure. Never a clean pass, in any branch.
    """
    name = type(exc).__name__
    message = str(exc)
    code = getattr(exc, "code", None)

    # Retryable, and both of these were seen on the first real run against gemini-3.7-flash:
    #
    #   429 RESOURCE_EXHAUSTED  our quota
    #   503 UNAVAILABLE         "this model is currently experiencing high demand"
    #
    # The second is the provider's capacity, not ours, and it is exactly the shape of a Space
    # still loading its weights: expected, transient, with a known recovery. Reporting it as a
    # hard failure would spend the caller's retry budget on a 502 that says "do not bother".
    if code in {429, 503} or "RESOURCE_EXHAUSTED" in message or "UNAVAILABLE" in message:
        return InferenceWarming(f"the model provider is busy: {exc}", 30)

    if name in {"ResourceExhausted", "TooManyRequests"}:
        return InferenceWarming(f"rate limited by the model provider: {exc}", 30)

    if isinstance(exc, (InferenceError, InferenceWarming)):
        return exc

    return InferenceError(f"the model call failed ({name}): {exc}")
