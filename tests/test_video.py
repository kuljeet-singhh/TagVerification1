"""
Video decoding, frame sampling and media-kind sniffing.

Fixtures are ENCODED AT TEST TIME rather than committed, so there are no opaque binaries in
the repo and each case states the content it is asserting about in the test itself. It also
means these tests need no network, no database and no model.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from tagverify.analyze import intake, video
from tests.support.videos import encode

ROOT_IMAGE = Path("inference/eval/alcohol/pos/Beer.jpg")

#: A flat scene to bracket a photo with. Distinct enough from any photograph that the
#: colour-aware dedupe keeps them apart, so a test that fails is the sampler's fault.
FLAT = "navy"


@pytest.fixture(scope="module")
def photo() -> Image.Image:
    return Image.open(ROOT_IMAGE).convert("RGB").resize((320, 180))


# ---------------------------------------------------------------------- sniff


def test_mp4_is_sniffed_as_video() -> None:
    assert video.looks_like_video(encode(["red"])) is True


def test_a_jpeg_is_not_sniffed_as_video() -> None:
    assert video.looks_like_video(ROOT_IMAGE.read_bytes()) is False


def test_the_kind_comes_from_the_bytes_not_the_name() -> None:
    """
    The load-bearing one. A caller must not be able to choose which checks run by renaming a
    file — that is how "upload a video instead" becomes a way around the policy.
    """
    assert intake.build(encode(["red", "green"]), ["alcohol"]).kind == "video"
    assert intake.build(ROOT_IMAGE.read_bytes(), ["alcohol"]).kind == "image"


# ------------------------------------------------------------------- sampling


def test_distinct_scenes_are_each_sampled() -> None:
    sampled = video.sample(encode(["red", "green", "blue"]))
    assert len(sampled.frames) == 3
    assert [round(t, 1) for t, _ in sampled.frames] == [0.0, 1.0, 2.0]
    assert sampled.duration_s == pytest.approx(3.0, abs=0.2)


def test_a_static_creative_collapses_to_one_frame() -> None:
    """
    The reason a typical DOOH creative costs the same as a still. A card that does not move
    has nothing to learn from its second through sixth frames.
    """
    assert len(video.sample(encode(["red"] * 6)).frames) == 1


def test_a_small_animating_element_is_still_one_scene() -> None:
    """A logo sliding across an otherwise static card is one scene, not six."""
    photo = Image.open(ROOT_IMAGE).convert("RGB").resize((320, 180))
    scenes: list[Image.Image] = []
    for step in range(6):
        frame = photo.copy()
        frame.paste(Image.new("RGB", (28, 28), "white"), (10 + step * 6, 10))
        scenes.append(frame)

    assert len(video.sample(encode(scenes, seconds_each=0.5)).frames) == 1


def test_flat_colour_scenes_are_not_merged() -> None:
    """
    Regression: an average hash greyscales and thresholds against the frame's OWN mean, so
    every flat-colour card hashes identically and different scenes silently merge. Flat colour
    backgrounds are everywhere in DOOH, so this has to stay a colour-aware comparison.
    """
    sampled = video.sample(encode(["red", "blue"]))
    assert len(sampled.frames) == 2


def test_sampling_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(video, "MAX_FRAMES", 3)
    scenes = ["red", "green", "blue", "yellow", "purple", "white", "black"]
    sampled = video.sample(encode(scenes, seconds_each=1.0, gop=10))
    assert len(sampled.frames) <= 3


def test_the_cap_keeps_the_first_and_last_frames() -> None:
    """A creative's opening and closing cards carry the brand and the legal small print."""
    frames = [(float(i), Image.new("RGB", (8, 8))) for i in range(10)]
    thinned = video._thin(frames, 4)
    assert thinned[0][0] == 0.0
    assert thinned[-1][0] == 9.0
    assert len(thinned) == 4


def test_a_single_keyframe_clip_falls_back_to_uniform_sampling() -> None:
    """
    Some encoders emit one keyframe and coast, leaving us no scene information at all. Without
    the fallback the whole clip would be judged on its first frame.
    """
    sampled = video.sample(encode(["red", "green", "blue"], gop=1000))
    assert len(sampled.frames) >= 2


def test_a_single_keyframe_clip_that_returns_to_its_opening_scene_is_still_sampled(
    photo: Image.Image,
) -> None:
    """
    The regression this file exists for, and the shape the old test could not catch.

    Every other multi-scene fixture here uses three DIFFERENT scenes, so a seek that always
    landed back on frame zero still happened to return something new further in. An A-B-A clip
    — open on the brand card, show the product, return to the brand card — defeated that: with
    one keyframe, every seek returned t=0, the de-dupe discarded them all as repeats, and an
    eight-second creative was judged on its opening frame with nothing reporting partial
    coverage. A real doughnut video shipped to a screen that blocks junk food.

    The middle scene is the whole point, so assert we actually reached it rather than merely
    counting frames.
    """
    clip = encode([FLAT, photo, FLAT], gop=1000, seconds_each=2.7)
    sampled = video.sample(clip)

    assert len(sampled.frames) >= 2
    assert any(t > 1.0 for t, _ in sampled.frames), (
        f"only sampled {[round(t, 2) for t, _ in sampled.frames]} of an 8.1s clip — "
        "the middle scene was never looked at"
    )


def test_sampling_does_not_require_a_declared_duration(
    photo: Image.Image, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A container that declares no duration must still be walked.

    Browser-generated MP4s frequently omit it, and the fallback used to be gated on
    `duration_s > 0` — so exactly those files skipped sampling and were judged on frame zero.
    Duration is a convenience for picking timestamps, never a prerequisite for looking.
    """
    clip = encode([FLAT, photo, FLAT], gop=1000, seconds_each=2.7)
    monkeypatch.setattr(
        video, "probe", lambda data: video.VideoInfo(duration_s=0.0, width=320, height=180)
    )

    sampled = video.sample(clip)
    assert len(sampled.frames) >= 2
    assert any(t > 1.0 for t, _ in sampled.frames)


def test_a_genuinely_static_card_still_collapses_to_one_frame(photo: Image.Image) -> None:
    """
    The fix must not turn every still into a six-frame charge.

    A video costs one rate-limit unit per frame upstream, so sampling a static card six times
    would sextuple the cost of the most common creative there is for no verdict at all.
    """
    assert len(video.sample(encode([photo, photo, photo], gop=1000)).frames) == 1


def test_frames_are_returned_in_timestamp_order() -> None:
    sampled = video.sample(encode(["red", "green", "blue", "yellow"]))
    stamps = [t for t, _ in sampled.frames]
    assert stamps == sorted(stamps)


# --------------------------------------------------------------------- limits


def test_undecodable_bytes_are_invalid_video() -> None:
    truncated = encode(["red", "green"])[:200]
    with pytest.raises(intake.MediaIntakeError) as caught:
        intake.build(truncated, ["alcohol"])
    assert caught.value.code == "INVALID_VIDEO"


def test_an_oversized_video_is_refused_before_decoding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(video, "MAX_VIDEO_BYTES", 128)
    with pytest.raises(intake.MediaIntakeError) as caught:
        intake.build(encode(["red", "green"]), ["alcohol"])
    assert caught.value.code == "VIDEO_TOO_LARGE"


def test_an_overlong_video_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(video, "MAX_DURATION_S", 1.0)
    with pytest.raises(intake.MediaIntakeError) as caught:
        intake.build(encode(["red", "green", "blue"]), ["alcohol"])
    assert caught.value.code == "VIDEO_TOO_LONG"


# --------------------------------------------------------------------- intake


def test_video_intake_carries_timestamps_and_indices() -> None:
    built = intake.build(encode(["red", "green", "blue"]), ["alcohol"])

    assert built.kind == "video"
    assert [f.index for f in built.frames] == [0, 1, 2]
    assert [round(f.timestamp_s, 1) for f in built.frames] == [0.0, 1.0, 2.0]
    assert built.duration_s == pytest.approx(3.0, abs=0.2)


def test_the_media_hash_is_of_the_original_bytes() -> None:
    """
    The cache key must identify what the caller submitted, not what we derived from it — the
    same rule the still path already follows.
    """
    import hashlib

    data = encode(["red", "green"])
    assert intake.build(data, ["alcohol"]).hash == hashlib.sha256(data).hexdigest()


def test_an_image_intake_is_a_single_frame_at_zero() -> None:
    """The still path is the one-frame case, not a separate branch."""
    built = intake.build(ROOT_IMAGE.read_bytes(), ["alcohol"])

    assert built.kind == "image"
    assert len(built.frames) == 1
    assert built.frames[0].timestamp_s == 0.0
    assert built.duration_s is None


def test_frames_are_downscaled_for_the_model() -> None:
    """Frames go through the same MAX_EDGE rule as stills — one answer, not two."""
    import base64

    built = intake.build(encode(["red", "green"], size=(1920, 1080)), ["alcohol"])
    decoded = Image.open(io.BytesIO(base64.b64decode(built.frames[0].base64)))
    assert max(decoded.size) <= intake.MAX_EDGE


# ------------------------------------------------------- partial-result guard




