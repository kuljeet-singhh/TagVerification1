"""
Turning a request into the frames we are going to score, plus a hash.

Accepts three shapes so callers can use whichever fits:
  - multipart/form-data with `media` (or `image`) and repeated `tags` fields
  - JSON { media_base64 | image_base64, tags[] }
  - JSON { media_url | image_url, tags[] }

...and two kinds of media. A still produces exactly one frame; a video produces up to
video.MAX_FRAMES of them, sampled by tagverify/analyze/video.py. Everything downstream — the
cache, the thresholds, the audit row — works in frames, so the image path is just the
one-frame case rather than a separate branch that has to be kept in step.

WHICH KIND IS DECIDED BY THE BYTES, NOT THE CALLER
--------------------------------------------------
Not the multipart field name, not the filename, not the Content-Type. All three are supplied
by the caller, and a compliance tool that lets the caller choose which checks run is not a
compliance tool: a video renamed `creative.jpg` must still be analysed as a video. The `image`
field name is kept working only because existing integrations send it.

This is the ONE intake path. The previous implementation had a second, partial copy of the
hashing and size checks inside the playground's server action, which is exactly how the two
entrypoints drift apart.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import ipaddress
import logging
import socket
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlparse

import anyio
import httpx
from PIL import Image, UnidentifiedImageError
from starlette.datastructures import FormData, UploadFile

from tagverify.analyze import video

log = logging.getLogger(__name__)

# The old 4MB cap existed purely because Vercel's serverless request bodies top out around
# 4.5MB. Off Vercel that constraint is gone, so the limit is now about protecting the model
# host, not the platform. The browser client downscales to 768px (~150KB), so anything
# approaching this is an API caller that skipped that step — which we now handle for them.
MAX_IMAGE_BYTES = 10 * 1024 * 1024

# 768px on the longest edge is what the model is shown, so detail beyond it is discarded
# either way -- and a 4K creative drops from ~8MB to ~150KB before it crosses the wire.
# Downscaling server-side gives API callers the same benefit the browser client already gives
# itself, and keeps the payload to the provider small, which is billed by the token.
#
# The number predates the VLM: it was set because SigLIP consumed 224x224 tiles and scored the
# full image plus nine half-size crops. It survived the change on its own merits, so do not
# read it as a leftover -- /docs documents 768 as the recommended edge for callers.
MAX_EDGE = 768
JPEG_QUALITY = 90

IntakeCode = Literal[
    "NO_IMAGE",
    "NO_TAGS",
    "IMAGE_TOO_LARGE",
    "INVALID_IMAGE",
    "INVALID_IMAGE_URL",
    "VIDEO_TOO_LARGE",
    "VIDEO_TOO_LONG",
    "INVALID_VIDEO",
    "BAD_REQUEST",
]


class MediaIntakeError(Exception):
    def __init__(self, code: IntakeCode, message: str) -> None:
        super().__init__(message)
        self.code: IntakeCode = code
        self.message = message


#: The old name. Kept because `except ImageIntakeError` appears in both route handlers and in
#: tests, and renaming a class is not worth touching those files for.
ImageIntakeError = MediaIntakeError


@dataclass(slots=True)
class Frame:
    """One picture to be scored, and where in the media it came from."""

    base64: str
    #: sha256 of this frame's ENCODED bytes. Used to drop frames that survived the visual
    #: dedupe but are byte-identical anyway; not a cache key (that is the media hash below).
    hash: str
    index: int
    #: Seconds into the video. Always 0.0 for a still.
    timestamp_s: float


@dataclass(slots=True)
class Intake:
    kind: Literal["image", "video"]
    #: sha256 of the raw SUBMITTED bytes — the cache key and the audit record.
    #: The media itself is never stored, whichever kind it is.
    hash: str
    bytes: int
    tags: list[str]
    #: Exactly one entry for a still.
    frames: list[Frame] = field(default_factory=list)
    duration_s: float | None = None
    #: Distinct frames the sampler found, before the MAX_FRAMES cap. Equal to
    #: len(frames) unless the cap truncated, so a caller can tell partial
    #: coverage from complete coverage instead of having to assume.
    frames_considered: int = 1


# ------------------------------------------------------------------------ SSRF


def _is_public(address: str) -> bool:
    """
    True only for addresses we are willing to fetch from.

    Python's `ipaddress` handles the whole taxonomy — including IPv4-mapped IPv6, which the
    previous hand-rolled check only half-covered by string-prefix matching "::ffff:" and then
    refusing outright rather than re-checking the embedded v4 address.
    """
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False  # unrecognised form: refuse

    # An IPv4-mapped v6 address is really a v4 address; judge it as one.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped

    return not (
        ip.is_private  # 10/8, 172.16/12, 192.168/16, fc00::/7 ...
        or ip.is_loopback  # 127/8, ::1
        or ip.is_link_local  # 169.254/16 (cloud instance metadata), fe80::/10
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified  # 0.0.0.0, ::
        # Carrier-grade NAT. Not "private" by Python's definition, but it is not somewhere a
        # public image lives either, and it can reach other tenants on some networks.
        or ip in ipaddress.ip_network("100.64.0.0/10")
    )


async def _assert_public_url(raw: str) -> str:
    """
    Reject URLs that point back into our own infrastructure.

    This endpoint fetches a URL on the caller's behalf, which without checks is a
    server-side request forgery primitive: someone could pass http://169.254.169.254/ to read
    cloud instance metadata, or a private address to probe internal services. We resolve the
    hostname and refuse unless EVERY resolved address is public.
    """
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        raise MediaIntakeError(
            "INVALID_IMAGE_URL",
            f"Only http and https URLs are supported, got {parsed.scheme or 'no scheme'}",
        )
    if not parsed.hostname:
        raise MediaIntakeError("INVALID_IMAGE_URL", f"Not a valid URL: {raw}")

    def _resolve() -> list[str]:
        return [info[4][0] for info in socket.getaddrinfo(parsed.hostname, None)]

    try:
        addresses = await anyio.to_thread.run_sync(_resolve)
    except OSError as exc:
        raise MediaIntakeError(
            "INVALID_IMAGE_URL", f"Could not resolve host {parsed.hostname}"
        ) from exc

    for address in addresses:
        if not _is_public(address):
            raise MediaIntakeError(
                "INVALID_IMAGE_URL",
                f"Refusing to fetch a private or loopback address ({parsed.hostname})",
            )
    return raw


# ------------------------------------------------------------------ normalising


def upload_from_form(form: FormData) -> UploadFile | None:
    """
    The first real file part in the form, whatever it happens to be called.

    `media` and `image` are the documented names and are checked first. The fallback is the
    actual point of this function: the handler no longer depends on the template and this
    module agreeing on a string literal held in two different files.

    That agreement broke once and cost three debugging sessions. A template renamed its input
    to `media` while a still-running server read only `image`; the request then looked
    file-less, and the user was told to "choose a creative" — the one thing they had
    definitely already done. The message pointed at the user, so the search went to the
    browser, the JavaScript and the cache, and never to the handler. Accepting any file part
    means a future rename degrades to "still works" rather than to a lie.

    `filename` is checked because a file input submitted with nothing selected still sends a
    part, with an empty filename. Accepting that would trade a clear "nothing attached" for a
    confusing zero-byte decode failure further down.
    """
    for name in ("media", "image"):
        value = form.get(name)
        if isinstance(value, UploadFile) and value.filename:
            return value

    for _, value in form.multi_items():
        if isinstance(value, UploadFile) and value.filename:
            return value
    return None


def form_field_names(form: FormData) -> str:
    """
    The field names a request actually carried, for error messages.

    Names only, never values — an error string is not a place to put creative bytes.
    """
    seen = list(dict.fromkeys(key for key, _ in form.multi_items()))
    return ", ".join(seen) if seen else "none"


def normalise_tags(value: Any) -> list[str]:
    """Accept a list or a comma-separated string; trim, drop empties, dedupe, keep order."""
    if isinstance(value, str):
        items: list[Any] = value.split(",")
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        items = []

    seen: dict[str, None] = {}
    for item in items:
        tag = str(item).strip()
        if tag:
            seen.setdefault(tag, None)

    tags = list(seen)
    if not tags:
        raise MediaIntakeError("NO_TAGS", "Provide at least one tag to verify.")
    return tags


def _downscale(image: Image.Image) -> tuple[Image.Image, str]:
    """
    Shrink to MAX_EDGE if needed. Returns (image, note) where note is "" if nothing happened.

    Shared by the still path and the video-frame path so there is exactly one answer to "what
    resolution does the model see", rather than two that drift.
    """
    width, height = image.size
    if max(width, height) <= MAX_EDGE:
        return image, ""

    scale = MAX_EDGE / max(width, height)
    resized = image.convert("RGB").resize(
        (max(1, round(width * scale)), max(1, round(height * scale))),
        Image.Resampling.LANCZOS,
    )
    return resized, f"downscaled from {width}x{height}"


def _encode(image: Image.Image) -> bytes:
    """JPEG-encode a frame for transport to the model."""
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return buffer.getvalue()


def _prepare(data: bytes) -> tuple[bytes, str]:
    """
    Validate the bytes are a real image and downscale if oversized.

    Returns (bytes_to_send, note). The hash is taken over the ORIGINAL bytes, before any
    re-encoding, so that the cache key and the audit record identify what the caller actually
    submitted rather than what we derived from it.
    """
    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()  # cheap structural check; consumes the file object
        with Image.open(io.BytesIO(data)) as image:
            resized, note = _downscale(image)
            if not note:
                return data, ""
            return _encode(resized), note
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise MediaIntakeError(
            "INVALID_IMAGE", "That does not decode as an image we can read."
        ) from exc


def _frame(payload: bytes, index: int, timestamp_s: float) -> Frame:
    return Frame(
        base64=base64.b64encode(payload).decode("ascii"),
        hash=hashlib.sha256(payload).hexdigest(),
        index=index,
        timestamp_s=timestamp_s,
    )


def _build_image(data: bytes, digest: str, tags: list[str]) -> Intake:
    if len(data) > MAX_IMAGE_BYTES:
        raise MediaIntakeError(
            "IMAGE_TOO_LARGE",
            f"Image is {round(len(data) / 1024)}KB; the limit is "
            f"{MAX_IMAGE_BYTES // 1024 // 1024}MB. Downscale before uploading — "
            f"{MAX_EDGE}px on the longest edge is plenty.",
        )

    payload, note = _prepare(data)
    if note:
        log.debug("intake %s: %s", digest[:12], note)

    return Intake(
        kind="image",
        hash=digest,
        bytes=len(data),
        tags=tags,
        frames=[_frame(payload, 0, 0.0)],
        frames_considered=1,
    )


def _build_video(data: bytes, digest: str, tags: list[str]) -> Intake:
    if len(data) > video.MAX_VIDEO_BYTES:
        raise MediaIntakeError(
            "VIDEO_TOO_LARGE",
            f"Video is {round(len(data) / 1024 / 1024)}MB; the limit is "
            f"{video.MAX_VIDEO_BYTES // 1024 // 1024}MB.",
        )

    try:
        info = video.probe(data)
    except video.VideoDecodeError as exc:
        raise MediaIntakeError("INVALID_VIDEO", str(exc)) from exc

    # Checked before sampling: refusing a 40-minute file should not cost a decode.
    if info.duration_s > video.MAX_DURATION_S:
        raise MediaIntakeError(
            "VIDEO_TOO_LONG",
            f"Video is {info.duration_s:.1f}s; the limit is "
            f"{video.MAX_DURATION_S:.0f}s. Ad creatives are far shorter than this.",
        )

    try:
        sampled = video.sample(data)
    except video.VideoDecodeError as exc:
        raise MediaIntakeError("INVALID_VIDEO", str(exc)) from exc

    frames: list[Frame] = []
    seen: set[str] = set()
    for timestamp, image in sampled.frames:
        resized, _ = _downscale(image)
        payload = _encode(resized)
        candidate = _frame(payload, len(frames), timestamp)
        # The visual dedupe in video.py works on the source frames; this catches the rarer
        # case of two frames that differ visually below the threshold but encode identically.
        # Scoring the same bytes twice is a wasted model call and nothing else.
        if candidate.hash in seen:
            continue
        seen.add(candidate.hash)
        frames.append(candidate)

    if not frames:
        raise MediaIntakeError("INVALID_VIDEO", "No frames could be read from that video.")

    log.debug(
        "intake %s: video %.1fs, %d frame(s) of %d considered",
        digest[:12], sampled.duration_s, len(frames), sampled.considered,
    )

    return Intake(
        kind="video",
        hash=digest,
        bytes=len(data),
        tags=tags,
        frames=frames,
        duration_s=round(sampled.duration_s, 3),
        frames_considered=max(sampled.considered, len(frames)),
    )


def build(data: bytes, tags: list[str]) -> Intake:
    """
    Finish an intake from raw bytes: validate, hash, sample, downscale, base64.

    The kind is sniffed from the bytes — see the note at the top of this module for why the
    caller does not get to declare it.
    """
    if not data:
        raise MediaIntakeError("INVALID_IMAGE", "The file was empty.")

    # Hash the ORIGINAL bytes. Two callers sending the same file must get the same cache key
    # whether or not our re-encoding step happened to be deterministic for them.
    digest = hashlib.sha256(data).hexdigest()

    if video.looks_like_video(data):
        return _build_video(data, digest, tags)
    return _build_image(data, digest, tags)


def decode_base64_media(value: str, *, field: str = "media_base64") -> bytes:
    """Accept raw base64 or a full `data:image/jpeg;base64,...` / `data:video/mp4;...` URL."""
    payload = value.split(",", 1)[1] if "," in value else value
    try:
        return base64.b64decode(payload.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise MediaIntakeError("INVALID_IMAGE", f"{field} is not valid base64.") from exc


#: Old name, still used by tests and any caller that imported it directly.
decode_base64_image = decode_base64_media


async def fetch_media_url(url: str) -> bytes:
    await _assert_public_url(url)
    try:
        async with httpx.AsyncClient(
            # A redirect could hop to a private address AFTER we validated the first one, so
            # refuse to follow them at all rather than re-validating each hop.
            follow_redirects=False,
            timeout=10.0,
        ) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        raise MediaIntakeError("INVALID_IMAGE_URL", f"Could not fetch image_url: {exc}") from exc

    if response.status_code >= 300:
        raise MediaIntakeError(
            "INVALID_IMAGE_URL", f"media_url returned HTTP {response.status_code}"
        )
    return response.content


#: Old name. The SSRF guard and the fetch are media-agnostic, so this is a rename only.
fetch_image_url = fetch_media_url


async def from_json(body: dict[str, Any]) -> Intake:
    tags = normalise_tags(body.get("tags"))

    # `media_*` is the current spelling; `image_*` is what existing integrations send. Both
    # accept either kind of media — the name is not what decides how the bytes are read.
    for field_name in ("media_base64", "image_base64"):
        value = body.get(field_name)
        if isinstance(value, str) and value.strip():
            return build(decode_base64_media(value, field=field_name), tags)

    for field_name in ("media_url", "image_url"):
        value = body.get(field_name)
        if isinstance(value, str) and value.strip():
            return build(await fetch_media_url(value.strip()), tags)

    raise MediaIntakeError(
        "NO_IMAGE",
        "Provide `media_base64` or `media_url` (`image_*` also accepted), "
        "or POST multipart/form-data.",
    )
