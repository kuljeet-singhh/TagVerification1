"""
POST /api/v1/analyze — the one endpoint that does the work, for stills and for video.

Both media kinds share this file because they share the endpoint and the pipeline: which one
a request is comes from the bytes, never the field name or the filename. Decode and sampling
behaviour is tested a level down in tests/test_video.py.

test_a_video_is_charged_per_frame and test_a_cached_video_is_charged_once are a pair — a
cache hit ran no inference and must stay one unit.
"""

from __future__ import annotations

import os

from tests.conftest import needs_db, needs_inference
from tests.support.videos import unique_two_scene_clip


@needs_db
@needs_inference
async def test_analyze_happy_path(client, api_key: str, beer_image: bytes) -> None:
    response = await client.post(
        "/api/v1/analyze",
        files={"image": ("beer.jpg", beer_image, "image/jpeg")},
        data={"tags": ["alcohol", "gym_fitness"]},
        headers={"x-api-key": api_key},
    )
    assert response.status_code == 200

    body = response.json()
    assert set(body) >= {
        "request_id", "image_hash", "image_bytes", "model",
        "packs_version", "cached", "latency_ms", "results",
    }
    assert len(body["image_hash"]) == 64  # sha256 hex
    assert {r["tag"] for r in body["results"]} == {"alcohol", "gym_fitness"}

    verdict = next(r for r in body["results"] if r["tag"] == "alcohol")
    assert verdict["present"] is True
    assert set(verdict) == {
        "tag", "present", "score", "confidence", "decided_by", "calibrated", "evidence",
    }
    assert set(verdict["evidence"]) == {"top_phrase", "crop", "sigmoid"}
    assert len(verdict["evidence"]["crop"]) == 4

    assert response.headers["x-ratelimit-limit"]
    assert response.headers["x-ratelimit-remaining"]


@needs_db
@needs_inference
async def test_near_miss_is_not_a_false_positive(client, api_key: str, juice_image: bytes) -> None:
    """A soft drink must not read as present for alcohol. Near-misses are the real test."""
    response = await client.post(
        "/api/v1/analyze",
        files={"image": ("drink.jpg", juice_image, "image/jpeg")},
        data={"tags": ["alcohol"]},
        headers={"x-api-key": api_key},
    )
    verdict = response.json()["results"][0]
    assert verdict["present"] is not True, f"soft drink scored {verdict['score']} for alcohol"


@needs_db
@needs_inference
async def test_cache_hit_on_a_repeat_request(client, api_key: str, beer_image: bytes) -> None:
    """
    Append bytes after the JPEG EOI marker so the sha256 differs while the DECODED image is
    identical — that guarantees a genuine cold miss for the first call rather than a cache
    hit left over from an earlier test run.
    """
    unique = beer_image + os.urandom(16)

    first = await client.post(
        "/api/v1/analyze",
        files={"image": ("beer.jpg", unique, "image/jpeg")},
        data={"tags": ["alcohol"]},
        headers={"x-api-key": api_key},
    )
    assert first.json()["cached"] is False

    second = await client.post(
        "/api/v1/analyze",
        files={"image": ("beer.jpg", unique, "image/jpeg")},
        data={"tags": ["alcohol"]},
        headers={"x-api-key": api_key},
    )
    body = second.json()
    assert body["cached"] is True
    assert body["results"][0]["score"] == first.json()["results"][0]["score"]


@needs_db
@needs_inference
async def test_tag_order_does_not_fragment_the_cache(
    client, api_key: str, beer_image: bytes
) -> None:
    unique = beer_image + os.urandom(16)
    common = {"headers": {"x-api-key": api_key}}

    await client.post(
        "/api/v1/analyze",
        files={"image": ("b.jpg", unique, "image/jpeg")},
        data={"tags": ["alcohol", "gym_fitness"]},
        **common,
    )
    reordered = await client.post(
        "/api/v1/analyze",
        files={"image": ("b.jpg", unique, "image/jpeg")},
        data={"tags": ["gym_fitness", "alcohol"]},
        **common,
    )
    assert reordered.json()["cached"] is True


# -------------------------------------------------------------------- video


@needs_db
@needs_inference
async def test_video_finds_a_tag_that_appears_in_one_scene(
    client, api_key: str, beer_video: bytes
) -> None:
    """
    The case the whole video path exists for.

    The beer is on screen for the middle second only. A first-frame check — which is what
    "analyse the still" amounts to for a video — would report this creative clean.
    """
    response = await client.post(
        "/api/v1/analyze",
        headers={"x-api-key": api_key},
        files={"media": ("creative.mp4", beer_video, "video/mp4")},
        data={"tags": ["alcohol"]},
    )
    assert response.status_code == 200

    body = response.json()
    verdict = body["results"][0]
    assert verdict["present"] is True

    # And it says WHERE, which is the difference between a verdict and something actionable.
    frame = verdict["evidence"]["frame"]
    assert frame["timestamp_s"] > 0.0
    assert body["media"]["kind"] == "video"
    assert body["media"]["frames_analyzed"] >= 2


@needs_db
@needs_inference
async def test_an_image_response_carries_no_video_keys(
    client, api_key: str, beer_image: bytes
) -> None:
    """
    The compatibility guarantee. Existing integrations parse this response; adding `media` or
    `evidence.frame` to it unconditionally would be a contract change dressed as a feature.
    """
    response = await client.post(
        "/api/v1/analyze",
        headers={"x-api-key": api_key},
        files={"image": ("creative.jpg", beer_image, "image/jpeg")},
        data={"tags": ["alcohol"]},
    )
    body = response.json()

    assert "media" not in body
    assert "frame" not in body["results"][0]["evidence"]


@needs_db
@needs_inference
async def test_a_video_sent_as_the_legacy_image_field_is_still_read_as_video(
    client, api_key: str, beer_video: bytes
) -> None:
    """
    The kind comes from the bytes. If it came from the field name or the filename, "upload it
    as an image instead" would be a way around the policy.
    """
    response = await client.post(
        "/api/v1/analyze",
        headers={"x-api-key": api_key},
        files={"image": ("creative.jpg", beer_video, "image/jpeg")},
        data={"tags": ["alcohol"]},
    )
    assert response.status_code == 200
    assert response.json()["media"]["kind"] == "video"


@needs_db
async def test_undecodable_video_is_its_own_error_code(client, api_key: str) -> None:
    """
    INVALID_VIDEO, not INVALID_IMAGE. A caller retrying with a different image because we told
    them the image was bad, when the real problem was a truncated upload, helps nobody.
    """
    # An MP4 header with nothing decodable behind it.
    broken = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 64
    response = await client.post(
        "/api/v1/analyze",
        headers={"x-api-key": api_key},
        files={"media": ("creative.mp4", broken, "video/mp4")},
        data={"tags": ["alcohol"]},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "INVALID_VIDEO"


@needs_db
@needs_inference
async def test_a_video_is_charged_per_frame(client, api_key: str) -> None:
    """
    A video that ran N model calls owes N units of quota, not 1.

    `guard` charges once per request, before the body is read — correct for a still, wrong
    for a video. Left uncorrected, sending a video is the cheap way to consume N times the
    capacity for the same quota, which widens the fail-open abuse surface the calling backend
    already worries about.

    The clip is randomised so this cannot silently become a test of the cache path: the result
    cache lives in Postgres and outlives the test run, so a fixed fixture would be a hit on
    every run after the first, and the branch being asserted here would never execute.
    """


    # Bytes nothing has seen before, so this is a genuine cache miss rather than a
    # replay — see unique_two_scene_clip for why the colours are chosen the way they are.
    video_bytes = unique_two_scene_clip()

    before = (await client.get("/api/v1/usage", headers={"x-api-key": api_key})).json()

    response = await client.post(
        "/api/v1/analyze",
        headers={"x-api-key": api_key},
        files={"media": ("creative.mp4", video_bytes, "video/mp4")},
        data={"tags": ["alcohol"]},
    )
    assert response.status_code == 200

    body = response.json()
    assert body["cached"] is False
    frames = body["media"]["frames_analyzed"]
    assert frames >= 2

    after = (await client.get("/api/v1/usage", headers={"x-api-key": api_key})).json()

    # Each usage read charges itself before reporting, so `before` already includes its own
    # unit and only the second read's falls between the two measurements.
    charged = after["today"]["requests"] - before["today"]["requests"] - 1
    assert charged == frames


@needs_db
@needs_inference
async def test_a_cached_video_is_charged_once(client, api_key: str, beer_video: bytes) -> None:
    """No model call was made, so there is nothing extra to charge."""
    await client.post(
        "/api/v1/analyze",
        headers={"x-api-key": api_key},
        files={"media": ("creative.mp4", beer_video, "video/mp4")},
        data={"tags": ["alcohol"]},
    )

    before = (await client.get("/api/v1/usage", headers={"x-api-key": api_key})).json()
    response = await client.post(
        "/api/v1/analyze",
        headers={"x-api-key": api_key},
        files={"media": ("creative.mp4", beer_video, "video/mp4")},
        data={"tags": ["alcohol"]},
    )
    assert response.json()["cached"] is True
    after = (await client.get("/api/v1/usage", headers={"x-api-key": api_key})).json()

    assert after["today"]["requests"] - before["today"]["requests"] - 1 == 1


@needs_db
@needs_inference
async def test_a_file_under_an_unexpected_field_name_is_still_analysed(
    client, api_key: str, beer_image: bytes
) -> None:
    """
    The skew that caused three rounds of debugging, pinned at the HTTP layer.

    A template sending `media` against a handler reading `image` made a perfectly good upload
    look file-less. The field name is no longer load-bearing, so a rename on either side
    degrades to "still works".
    """
    response = await client.post(
        "/api/v1/analyze",
        headers={"x-api-key": api_key},
        files={"creative": ("creative.jpg", beer_image, "image/jpeg")},
        data={"tags": ["alcohol"]},
    )
    assert response.status_code == 200
    assert response.json()["results"][0]["tag"] == "alcohol"


@needs_db
async def test_a_missing_file_names_the_fields_that_did_arrive(client, api_key: str) -> None:
    """
    An error that misdescribes its own cause is a defect. The old message told the user to
    attach a file they had already attached, and said nothing about what the server actually
    received — so the search went everywhere except the handler.
    """
    # `files=` with a (None, value) part makes httpx send multipart WITHOUT a file, which is
    # exactly the shape the bug produced: a well-formed multipart body carrying every field
    # except the one the handler was looking for.
    response = await client.post(
        "/api/v1/analyze",
        headers={"x-api-key": api_key},
        files={"tags": (None, "alcohol")},
    )
    body = response.json()

    assert response.status_code == 400
    assert body["error"] == "NO_IMAGE"  # the contract code must not change
    assert "tags" in body["message"]    # ...but it now says what turned up


@needs_db
async def test_an_empty_file_input_is_reported_as_no_file(client, api_key: str) -> None:
    """A file input with nothing selected still sends a part; it is not an upload."""
    response = await client.post(
        "/api/v1/analyze",
        headers={"x-api-key": api_key},
        files={"media": ("", b"", "application/octet-stream")},
        data={"tags": ["alcohol"]},
    )
    assert response.json()["error"] == "NO_IMAGE"
