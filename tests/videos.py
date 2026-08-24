"""
Building test videos in memory.

The MP4 encoder was written out three times — in `test_video.py`, in `conftest.py`'s
`beer_video`, and again inline in `test_api.py`'s per-frame billing test. Three copies of
`av.open(..., "w", format="mp4")` with slightly different sizes and GOP settings is the same
drift risk `banding.py`'s header describes, and the 20-tag video sweep would have made it four.

Encoded rather than committed, for the reason `beer_video` gave first: a binary fixture in the
repo is opaque, and this way the thing being asserted about — "the content is only in the
middle second" — is stated in code.

Nothing here needs a model or a network. `av`'s wheels bundle ffmpeg, so `libx264` is
available without a system package.
"""

from __future__ import annotations

import io
import secrets
from pathlib import Path

import av
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent

#: Small, because the sampler works on an 8x8 colour signature and the model resizes to 224
#: anyway. Encoding at creative resolution would cost seconds per clip and prove nothing.
SIZE = (320, 180)


def encode(
    scenes: list[str | Image.Image],
    *,
    seconds_each: float = 1.0,
    fps: int = 10,
    gop: int = 10,
    size: tuple[int, int] = SIZE,
) -> bytes:
    """
    An in-memory MP4 of one scene after another.

    `gop` sets the keyframe interval, which is what the sampler reads as a scene cut — pass a
    large one to build a clip whose encoder emitted a single keyframe, and the uniform-sampling
    fallback in `dooh/analyze/video.py` takes over.

    Scenes may be `PIL` images or colour names (`"red"`), so a test that only cares about
    "two distinguishable scenes" does not have to source photographs.
    """
    buffer = io.BytesIO()
    container = av.open(buffer, "w", format="mp4")
    stream = container.add_stream("libx264", rate=fps)
    stream.width, stream.height = size
    stream.pix_fmt = "yuv420p"
    stream.options = {"g": str(gop)}

    for scene in scenes:
        image = scene if isinstance(scene, Image.Image) else Image.new("RGB", size, scene)
        image = image.convert("RGB").resize(size)
        for _ in range(int(seconds_each * fps)):
            for packet in stream.encode(av.VideoFrame.from_image(image)):
                container.mux(packet)
    for packet in stream.encode():
        container.mux(packet)
    container.close()
    return buffer.getvalue()


def middle_scene_clip(subject: Image.Image, neutral: Image.Image, **kwargs: object) -> bytes:
    """
    Three seconds, with `subject` on screen for the middle one only.

    This is the case the whole video path exists for. A first-frame check — which is what
    "analyse the still" amounts to for a video — reports this creative clean, so a tag that
    passes on the still and fails here has a real bug between the two.

    The verdict should be `present` with `evidence.frame.timestamp_s` landing in [1.0, 2.0).
    """
    return encode([neutral, subject, neutral], **kwargs)  # type: ignore[arg-type]


def unique_two_scene_clip() -> bytes:
    """
    A clip with bytes nothing has seen before, for testing a genuine cache MISS.

    Two randomised but strongly separated colours: random so the sha256 is new, far apart so
    the frame dedupe correctly keeps both as scenes. Random *noise* would not work — it
    averages to the same mid-grey on the comparison grid and would legitimately collapse to
    one frame.
    """
    dark = tuple(secrets.randbelow(60) for _ in range(3))
    light = tuple(195 + secrets.randbelow(60) for _ in range(3))
    return encode([Image.new("RGB", SIZE, dark), Image.new("RGB", SIZE, light)])


def truncated(data: bytes, keep: int = 200) -> bytes:
    """The head of a real MP4 — decodable magic, undecodable body. Must never pass silently."""
    return data[:keep]


def neutral_image() -> Image.Image:
    """
    A photograph that belongs to no tag, for the scenes either side of the subject.

    A flat colour would be wrong here: the sampler's dedupe is colour-aware, so a solid frame
    next to a photo is trivially separable in a way real creatives are not, and the model's
    sigmoid floor treats a featureless frame differently from a real one.
    """
    return Image.open(ROOT / "inference/.testimages/Mount_Everest.jpg").convert("RGB")
