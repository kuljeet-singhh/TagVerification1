"""
Turning a video creative into the handful of frames worth scoring.

A creative tagged `alcohol` is just as non-compliant if the bottle is only on screen for one
second of a ten-second loop, so "analyse the first frame" is not a check — it is a coin toss
with a compliance report attached. This module decides WHICH frames get scored.

WHY KEYFRAMES
-------------
Decoding every frame of a 10s clip is ~300 images to encode, and almost all of them are
near-identical to their neighbours. Keyframes (I-frames) are where the encoder itself decided
the picture changed enough to stop predicting from the previous one — which is as close to a
free scene-cut detector as exists. `skip_frame = "NONKEY"` makes the decoder throw the rest
away before they are ever reconstructed, so this is dramatically cheaper than decoding and
then filtering.

Some encoders emit one keyframe and coast, so a clip with fewer than two gets a second pass
that seeks to evenly spaced timestamps instead. Either way the caller gets timestamps.

WHY DEDUPE AFTER THAT
---------------------
A DOOH card is very often a still image with a logo animating in a corner. Its keyframes are
all the same picture, and scoring six copies of one frame costs six model calls to learn what
one would have told us. The signature pass collapses those to one. This is the single
reason a typical creative costs the same as a still.

NOTHING IS WRITTEN TO DISK. Decoding is from an in-memory buffer, so the "never store the
media, only its sha256" rule in the README holds for video exactly as it did for images.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass

import av
from PIL import Image

log = logging.getLogger(__name__)

# The backend caps creative videos at 50MB (creative-upload-rules.ts), so this matches rather
# than inventing a second number for the same thing.
MAX_VIDEO_BYTES = 50 * 1024 * 1024

# Creatives are capped at 10s upstream. This is headroom for direct API callers, and a guard
# against someone posting a feature film for us to decode.
MAX_DURATION_S = 60.0

# The latency budget, not a quality knob. Each frame is one round trip to the Space at ~1s
# warm, and the calling backend gives a video upload a 35s deadline (VIDEO_BATCH_DEADLINE_MS
# in creative-verification.service.ts), sized against the 30s TOTAL_BUDGET_S in run.py.
# Raising this without raising both of those just converts verdicts into timeouts.
#
# The backend also reserves this many rate-limit units per video before it calls, because the
# per-key counter is charged per frame. Raising it there is part of raising it here.
MAX_FRAMES = 6

# Two frames closer together than this are the same moment for our purposes.
MIN_FRAME_GAP_S = 0.35

# Mean absolute difference, per colour channel on a 0-255 scale, below which two frames are
# "the same picture". Tolerates compression noise and a small animating element (a logo in one
# corner moves only one or two cells of a 64-cell grid) while separating genuine scene cuts.
SIGNATURE_DISTANCE = 8.0

#: Grid the frame is reduced to before comparison. 8x8 in colour = 192 numbers per frame.
SIGNATURE_GRID = 8

#: Container magic. MP4/MOV carry `ftyp` at offset 4; Matroska/WebM open with an EBML header.
_FTYP = b"ftyp"
_EBML = b"\x1a\x45\xdf\xa3"


class VideoDecodeError(Exception):
    """The bytes are a video we cannot read, or contain no usable video stream."""


@dataclass(slots=True)
class VideoInfo:
    duration_s: float
    width: int
    height: int


def looks_like_video(data: bytes) -> bool:
    """
    Sniff the container from the bytes themselves.

    Deliberately not the multipart field name or the client's content-type: both are supplied
    by the caller, and this file already refuses to trust caller-supplied framing anywhere
    else (see the Pillow `verify()` in intake.py). A `.mp4` named `creative.jpg` must still be
    read as a video, and a JPEG posted as `video/mp4` must still be read as an image.
    """
    return data[:4] == _EBML or data[4:8] == _FTYP


# ------------------------------------------------------------------- decoding


def _duration_of(
    container: av.container.InputContainer,
    stream: av.video.stream.VideoStream,
) -> float:
    """
    Best available duration, in seconds.

    The stream's own duration is the more accurate of the two but is absent in plenty of
    real-world files, so the container's is the fallback. Zero means "unknown", not "empty" —
    callers treat it as unknown rather than refusing the file.
    """
    if stream.duration is not None and stream.time_base:
        return float(stream.duration * stream.time_base)
    if container.duration is not None:
        return float(container.duration) / av.time_base
    return 0.0


def probe(data: bytes) -> VideoInfo:
    """Duration and dimensions, without decoding any picture data."""
    try:
        with av.open(io.BytesIO(data)) as container:
            if not container.streams.video:
                raise VideoDecodeError("The file contains no video stream.")
            stream = container.streams.video[0]
            return VideoInfo(
                duration_s=_duration_of(container, stream),
                width=stream.codec_context.width,
                height=stream.codec_context.height,
            )
    except VideoDecodeError:
        raise
    except Exception as exc:  # av raises a wide family of its own error types
        raise VideoDecodeError(f"That does not decode as a video we can read: {exc}") from exc


def _decode_keyframes(data: bytes) -> list[tuple[float, Image.Image]]:
    """Every I-frame, as (timestamp_seconds, image). Cheap: nothing else is reconstructed."""
    frames: list[tuple[float, Image.Image]] = []
    with av.open(io.BytesIO(data)) as container:
        stream = container.streams.video[0]
        stream.codec_context.skip_frame = "NONKEY"
        for frame in container.decode(stream):
            if frame.pts is None:
                continue
            frames.append((float(frame.pts * stream.time_base), frame.to_image()))
    return frames


def _spread_indices(total: int, count: int) -> list[int]:
    """`count` indices spread evenly across `total`, always including the first and the last."""
    if total <= count:
        return list(range(total))
    if count < 2:
        return [0]
    step = (total - 1) / (count - 1)
    return sorted({round(index * step) for index in range(count)})


def _decode_spread(data: bytes, count: int) -> list[tuple[float, Image.Image]]:
    """
    Up to `count` frames spread across the clip, reached by DECODING FORWARD.

    WHY NOT SEEKING
    ---------------
    This replaces a seek-per-target implementation, and the distinction is the whole bug it
    fixes. This function is only ever called when the encoder emitted fewer than two keyframes
    — and a seek can only land on a keyframe. With exactly one, every seek returned the opening
    frame, the de-dupe discarded them all as repeats of the frame already decoded, and an
    eight-second creative was judged on its first frame alone. Worse, it was silent: one frame
    in, one frame considered, indistinguishable from a genuinely static card.

    Decoding forward is the only way to reach 3.0s of a single-keyframe clip. It also needs no
    duration, which matters because a browser-generated MP4 frequently declares none, and the
    old guard skipped sampling entirely when it could not read one.

    TWO PASSES, ON PURPOSE
    ----------------------
    The first pass counts frames and their timestamps; the second converts only the ones we
    keep. Decoding an in-memory buffer twice is cheap beside a single model call, whereas
    turning every frame of a 30fps clip into a PIL image is not — and holding decoded frames
    from the first pass is not an option, since PyAV reuses their buffers.
    """
    timestamps: list[float] = []
    with av.open(io.BytesIO(data)) as container:
        stream = container.streams.video[0]
        for frame in container.decode(stream):
            if frame.pts is not None:
                timestamps.append(float(frame.pts * stream.time_base))

    if not timestamps:
        return []

    wanted = set(_spread_indices(len(timestamps), count))

    frames: list[tuple[float, Image.Image]] = []
    with av.open(io.BytesIO(data)) as container:
        stream = container.streams.video[0]
        index = 0
        for frame in container.decode(stream):
            if frame.pts is None:
                continue
            if index in wanted:
                frames.append((float(frame.pts * stream.time_base), frame.to_image()))
                if len(frames) == len(wanted):
                    break
            index += 1
    return frames


# ------------------------------------------------------------------- sampling


def signature(image: Image.Image) -> tuple[int, ...]:
    """
    A frame's visual fingerprint: an 8x8 grid of average RGB, 192 numbers.

    WHY NOT AVERAGE HASH
    --------------------
    The obvious choice here is a 64-bit average hash — greyscale, one bit per cell above the
    frame's own mean. It is wrong for this content. A flat-colour card has every cell equal to
    its own mean, so every bit is 0: a solid red frame and a solid blue frame produce the SAME
    hash. DOOH creatives are full of flat colour backgrounds, so that failure is not a corner
    case here, and its consequence is silent — the second scene is dropped as a duplicate and
    never scored, and the response still says we analysed the video.

    Keeping the actual colour values fixes both blind spots at once: greyscale conversion
    (which loses red-vs-blue) and self-relative thresholding (which loses light-vs-dark).
    """
    small = image.convert("RGB").resize(
        (SIGNATURE_GRID, SIGNATURE_GRID), Image.Resampling.LANCZOS
    )
    return tuple(value for pixel in small.getdata() for value in pixel)


def looks_the_same(left: tuple[int, ...], right: tuple[int, ...]) -> bool:
    """Mean absolute difference across every cell and channel, against SIGNATURE_DISTANCE."""
    if len(left) != len(right):
        return False
    total = sum(abs(a - b) for a, b in zip(left, right, strict=True))
    return (total / len(left)) <= SIGNATURE_DISTANCE


def _dedupe(frames: list[tuple[float, Image.Image]]) -> list[tuple[float, Image.Image]]:
    """
    Drop frames that look like one we are already keeping, and frames too close in time.

    Compared against EVERY kept frame rather than only the previous one: a creative that cuts
    A -> B -> A would otherwise keep two copies of A.
    """
    kept: list[tuple[float, Image.Image]] = []
    signatures: list[tuple[int, ...]] = []

    for timestamp, image in frames:
        if kept and timestamp - kept[-1][0] < MIN_FRAME_GAP_S:
            continue
        current = signature(image)
        if any(looks_the_same(current, seen) for seen in signatures):
            continue
        kept.append((timestamp, image))
        signatures.append(current)

    return kept


def _thin(frames: list[tuple[float, Image.Image]], limit: int) -> list[tuple[float, Image.Image]]:
    """
    Reduce to `limit` frames, spread evenly, always keeping the first and the last.

    The ends matter disproportionately: a creative's opening and closing cards are where the
    brand and the legal small print live.
    """
    if len(frames) <= limit:
        return frames
    if limit == 1:
        return [frames[0]]

    step = (len(frames) - 1) / (limit - 1)
    picked = {round(index * step) for index in range(limit)}
    return [frames[index] for index in sorted(picked)]


@dataclass(slots=True)
class Sampled:
    frames: list[tuple[float, Image.Image]]
    duration_s: float
    #: How many distinct frames the sampler found before the MAX_FRAMES cap was applied.
    #: Reported to the caller so "we looked at 6 of 11 scenes" is never mistaken for
    #: "we looked at everything".
    considered: int


def sample(data: bytes) -> Sampled:
    """
    The frames worth scoring, in timestamp order.

    Raises VideoDecodeError if nothing at all could be decoded. That is deliberately an error
    and not an empty list: a caller handed an empty result would report "no tags present",
    which is "we didn't check" wearing the costume of "we checked and it's clean".
    """
    info = probe(data)

    try:
        frames = _decode_keyframes(data)
    except Exception as exc:  # noqa: BLE001
        raise VideoDecodeError(f"Could not decode the video: {exc}") from exc

    # Fewer than two keyframes means the encoder gave us no scene information at all, so fall
    # back to walking the clip. Anything already decoded is kept and merged in.
    #
    # Deliberately NOT conditioned on a known duration. It used to be, and a container that
    # declares none — which browser-generated MP4s often do — skipped sampling altogether and
    # got judged on its opening frame. Duration is a convenience for picking timestamps, never
    # a prerequisite for looking at the video.
    if len(frames) < 2:
        extra = _decode_spread(data, MAX_FRAMES)
        seen = {round(timestamp, 3) for timestamp, _ in frames}
        frames.extend((t, image) for t, image in extra if round(t, 3) not in seen)
        frames.sort(key=lambda item: item[0])

    if not frames:
        raise VideoDecodeError("No frames could be decoded from that video.")

    unique = _dedupe(frames)
    return Sampled(
        frames=_thin(unique, MAX_FRAMES),
        duration_s=info.duration_s,
        considered=len(unique),
    )
