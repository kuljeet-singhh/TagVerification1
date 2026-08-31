"""
The ONLY module that knows the inference service is a Gradio Space.

Everything else calls `analyze_image()` and gets typed data back. If we ever move to
FastAPI, a different host, or a paid GPU, this file changes and nothing else does.

Why gradio_client instead of raw HTTP: Gradio's HTTP API is a three-step protocol (upload
file, POST for an event_id, then parse a Server-Sent Events stream). The official client
hides that. We send the image as base64 rather than a file upload, which skips the upload
step entirely — one round trip.

gradio_client is synchronous, so every call is pushed to a worker thread. Blocking the event
loop on a multi-second model call would stall every other request in the process, which
matters far more here than it did on serverless where each request had its own instance.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import anyio
from pydantic import BaseModel, Field, ValidationError

from tagverify.config import settings

log = logging.getLogger(__name__)


# --------------------------------------------------------------------- schemas


class RawVerdict(BaseModel):
    """
    One tag's raw result from the model.

    The Space applies its own provisional thresholds and returns `present` / `band` /
    `confidence`, but we ignore its verdict and re-decide from `score` and `sigmoid` using
    thresholds in Postgres. See tagverify/tags/decide.py for why. Those fields are still parsed so
    that a shape change in the Space is caught here rather than surfacing as a KeyError deep
    in the pipeline.
    """

    tag: str
    present: bool | None
    score: float
    sigmoid: float
    band: str
    confidence: str
    decided_by: str
    top_phrase: str
    crop: list[float] = Field(min_length=4, max_length=4)


class RawAnalysis(BaseModel):
    model: str
    packs_version: str
    latency_ms: float
    image_size: list[int] = Field(min_length=2, max_length=2)
    results: list[RawVerdict]


class InferenceHealth(BaseModel):
    status: str
    model: str
    packs_version: str
    tags: list[str]
    prompts: int
    #: slug -> digest of the phrases and floor that model is actually scoring that tag with.
    #: DEFAULTED, because the two tiers deploy on their own schedules: a Space still running
    #: older code must not turn every admin page load into "Cannot tell what is live". None
    #: therefore means "this model cannot tell us", NOT "nothing has drifted" -- the same
    #: distinction live_slugs=None carries, and for the same reason.
    prompt_fingerprints: dict[str, str] | None = None


class TagInfo(BaseModel):
    slug: str
    label: str
    description: str


class TagCatalog(BaseModel):
    packs_version: str
    tags: list[TagInfo]


# ---------------------------------------------------------------------- errors


class InferenceWarming(Exception):
    """
    The Space is asleep or still loading the model.

    Callers should surface a retryable 503, not a generic failure. A free Space sleeps after
    48h idle and a cold start re-downloads ~400MB of weights, so this is an expected
    condition with a known recovery, not an error.
    """

    def __init__(self, message: str, retry_after: int = 30) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class InferenceError(Exception):
    """The service is reachable but the call failed or returned something unrecognisable."""


class PublishNotConfigured(Exception):
    """
    Publishing is switched off because no shared secret is set.

    Deliberately NOT an InferenceError. That one means "the model tried and failed", which
    invites a retry; this means "you have not configured this", where retrying is pointless
    and the fix is an operator's. Reporting the second as the first sends people to look at
    the model tier for a problem that is in their environment file.
    """


# -------------------------------------------------------------------- plumbing

# Warm requests take ~600ms-2s. A cold Space has to download and load ~400MB of weights, so
# we give up early and report "warming" rather than holding a connection open for a minute.
CALL_TIMEOUT_S = 8.0
CONNECT_TIMEOUT_S = 6.0

# Publishing a pack re-encodes the WHOLE prompt table on the Space's CPU -- measured at 4.8s
# for 266 prompts and growing with the catalog -- so it does not fit in the read budget above.
# Its own number, because raising CALL_TIMEOUT_S would mean every scoring request waits eight
# times longer to find out the Space is asleep.
RELOAD_TIMEOUT_S = 60.0

_client: Any = None
_connect_lock = asyncio.Lock()


def target() -> str:
    t = settings().inference_target
    if not t:
        raise InferenceError(
            "Neither HF_SPACE nor INFERENCE_URL is set — cannot reach the inference service"
        )
    return t


def _connect_sync() -> Any:
    from gradio_client import Client

    token = (settings().hf_token or "").strip()
    # The token is omitted entirely when absent, because passing an empty one is treated as
    # a malformed credential rather than as "no credential".
    kwargs: dict[str, Any] = {"hf_token": token} if token else {}
    return Client(target(), **kwargs)


async def _connected() -> Any:
    """
    Cached client handshake.

    The lock (rather than a bare check) means concurrent requests during a cold start share
    one handshake instead of stampeding the Space with N connection attempts. A FAILED
    handshake is never cached, or one blip would poison the process until it restarts.
    """
    global _client
    if _client is not None:
        return _client

    async with _connect_lock:
        if _client is not None:
            return _client
        try:
            with anyio.fail_after(CONNECT_TIMEOUT_S):
                _client = await anyio.to_thread.run_sync(_connect_sync)
        except TimeoutError as exc:
            raise InferenceWarming(f"connect exceeded {CONNECT_TIMEOUT_S}s") from exc
        except InferenceError:
            raise
        except Exception as exc:
            raise InferenceWarming(f"could not reach inference service: {exc}") from exc
        return _client


def reset_client() -> None:
    """Force a fresh handshake — used after a dropped connection, i.e. a Space restart."""
    global _client
    _client = None


async def _call(endpoint: str, *payload: Any, timeout: float = CALL_TIMEOUT_S) -> Any:
    client = await _connected()

    def _run() -> Any:
        return client.predict(*payload, api_name=f"/{endpoint}")

    try:
        with anyio.fail_after(timeout):
            return await anyio.to_thread.run_sync(_run)
    except TimeoutError as exc:
        raise InferenceWarming(f"/{endpoint} exceeded {timeout}s") from exc
    except Exception as exc:
        # A dropped connection usually means the Space restarted; force a fresh handshake.
        reset_client()
        raise InferenceError(f"{endpoint} failed: {exc}") from exc


# ------------------------------------------------------------------ public API


async def analyze_image(image_base64: str, tags: list[str]) -> RawAnalysis:
    """
    Score an image against tags.

    `image_base64` may be raw base64 or a full `data:` URL — the Space accepts both.
    Unknown slugs are rejected by the Space; we check them earlier anyway so that no
    inference is paid for a request that will be refused.
    """
    raw = await _call("analyze", image_base64, tags)
    try:
        return RawAnalysis.model_validate(raw)
    except ValidationError as exc:
        # The Space returned something we don't understand — a version skew, or a Gradio
        # error object. Fail loudly rather than guessing at the shape.
        raise InferenceError(f"unexpected response from inference service: {exc}") from exc


async def inference_health() -> InferenceHealth:
    try:
        return InferenceHealth.model_validate(
            await _call(
                "health",
            )
        )
    except ValidationError as exc:
        raise InferenceError(f"unexpected health response: {exc}") from exc


async def tag_catalog() -> TagCatalog:
    try:
        return TagCatalog.model_validate(
            await _call(
                "tags",
            )
        )
    except ValidationError as exc:
        raise InferenceError(f"unexpected tags response: {exc}") from exc


def require_publishing_configured() -> str:
    """
    The shared secret, or a refusal. Separate from push_packs so a caller can find out BEFORE
    doing work it would have to undo — see tags/publish.py, which writes a file.
    """
    secret = (settings().reload_secret or "").strip()
    if not secret:
        raise PublishNotConfigured(
            "RELOAD_SECRET is not set — refusing to publish. Set it here and as RELOAD_SECRET "
            "on the model tier, or export the pack and restart the model instead."
        )
    return secret


async def push_packs(pack_bytes: bytes) -> InferenceHealth:
    """
    Publish a pack into the RUNNING model, so a new tag goes live without a restart.

    Takes BYTES, and they must be the exporter's own: packs_version is a hash of them, so a
    re-serialised pack would fingerprint differently from the file `dooh export-packs` wrote
    and `dooh apply-calibration` would then refuse the calibration measured against it.

    Clears the memos on success. That is not tidiness -- analyze/run.py stamps
    `cached_inference_health().packs_version` onto both the audit row and the score-cache row,
    so for up to _TTL_S after a reload, scores computed under the NEW pack would be persisted
    labelled with the OLD one. Wrong-fingerprint rows are exactly what versioning.py exists to
    make impossible, and they would be served as current until they expired.
    """
    secret = require_publishing_configured()

    raw = await _call("reload_packs", pack_bytes.decode(), secret, timeout=RELOAD_TIMEOUT_S)
    try:
        health = InferenceHealth.model_validate(raw)
    except ValidationError as exc:
        raise InferenceError(f"unexpected reload response: {exc}") from exc

    # Only after the model confirmed the swap. Clearing on failure would replace a known-good
    # memo with a re-read of the same unchanged pack, for nothing.
    reset_caches()
    return health


# ------------------------------------------------------------ memoised reads
#
# /analyze needs `packs_version` (it is part of the cache key) and the tag list (to reject
# unknown tags) before it can do anything. Both only change when the Space is redeployed, so
# a short TTL is safe and saves a full network round trip per request — which used to be the
# difference between a 280ms cache hit and a 900ms one.
#
# The window means that immediately after a Space deploy we may serve up to a minute of
# scores keyed to the previous pack version. Acceptable; the alternative is paying a round
# trip forever.
#
# The catalog is memoised too. The previous implementation memoised health but not the
# catalog, so GET /api/v1/tags and every playground render paid a Space round trip.

_TTL_S = 60.0
_health_cache: tuple[float, InferenceHealth] | None = None
_catalog_cache: tuple[float, TagCatalog] | None = None


async def cached_inference_health() -> InferenceHealth:
    global _health_cache
    if _health_cache and (time.monotonic() - _health_cache[0]) < _TTL_S:
        return _health_cache[1]
    value = await inference_health()
    _health_cache = (time.monotonic(), value)
    return value


async def cached_tag_catalog() -> TagCatalog:
    global _catalog_cache
    if _catalog_cache and (time.monotonic() - _catalog_cache[0]) < _TTL_S:
        return _catalog_cache[1]
    value = await tag_catalog()
    _catalog_cache = (time.monotonic(), value)
    return value


def reset_caches() -> None:
    global _health_cache, _catalog_cache
    _health_cache = None
    _catalog_cache = None
