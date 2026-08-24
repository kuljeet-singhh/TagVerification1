"""GET /docs — the API reference."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from dooh.analyze import video
from dooh.analyze.intake import MAX_IMAGE_BYTES
from dooh.inference.client import cached_inference_health
from dooh.templating import render

router = APIRouter()

#: (code, status, meaning). The previous docs table omitted four codes the API can
#: actually return, which meant a caller branching on `error` could hit one that was
#: not documented anywhere.
ERRORS: list[tuple[str, int, str]] = [
    ("MISSING_KEY", 401, "No key was presented"),
    ("INVALID_KEY", 401, "The key is unknown or revoked — the two are indistinguishable by design"),
    ("RATE_LIMITED", 429, "Over your per-minute limit; see the Retry-After header"),
    ("UNKNOWN_TAG", 400, "A tag slug was not recognised — never treated as 'absent'"),
    ("NO_TAGS", 400, "No tags were supplied"),
    ("NO_IMAGE", 400, "No file was supplied"),
    ("INVALID_IMAGE", 400, "The bytes do not decode as an image"),
    ("IMAGE_TOO_LARGE", 413, f"Over {MAX_IMAGE_BYTES // 1024 // 1024}MB — downscale first"),
    ("INVALID_IMAGE_URL", 400, "Unfetchable, or resolves to a private or loopback address"),
    ("INVALID_VIDEO", 400, "The bytes do not decode as a video, or it has no readable frames"),
    (
        "VIDEO_TOO_LARGE",
        413,
        f"Over {video.MAX_VIDEO_BYTES // 1024 // 1024}MB",
    ),
    (
        "VIDEO_TOO_LONG",
        413,
        f"Longer than {video.MAX_DURATION_S:.0f}s — ad creatives are far shorter",
    ),
    ("BAD_REQUEST", 400, "Malformed body or an unsupported content type"),
    ("INFERENCE_WARMING", 503, "The model service is waking up; retry after Retry-After"),
    ("INFERENCE_FAILED", 502, "The model service errored"),
    ("INTERNAL", 500, "Unexpected failure on our side"),
]


@router.get("/docs", response_class=HTMLResponse)
async def docs(request: Request) -> HTMLResponse:
    # Quote the live pack fingerprint rather than a hardcoded one, so the example response
    # cannot drift from what the service actually returns.
    # Quoted in the request-field list too, so the documented cap and the enforced one
    # cannot drift apart.
    context: dict[str, object] = {
        "errors": ERRORS,
        "max_image_mb": MAX_IMAGE_BYTES // 1024 // 1024,
        "max_video_mb": video.MAX_VIDEO_BYTES // 1024 // 1024,
        "max_video_seconds": int(video.MAX_DURATION_S),
        "max_frames": video.MAX_FRAMES,
    }
    try:
        health = await cached_inference_health()
        context |= {
            "packs_version": health.packs_version,
            "model": health.model,
            "health_class": "is-ok",
        }
    except Exception:  # noqa: BLE001 - the docs must render whether or not the Space is up
        context["health_class"] = "is-down"

    return render(request, "pages/docs.html", context)
