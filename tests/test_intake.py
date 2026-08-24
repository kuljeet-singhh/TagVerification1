"""
Image intake: parsing, size limits, and the SSRF guard.

The SSRF cases are the important ones. `image_url` makes the server fetch a URL on the
caller's behalf, which without checks is a way to read cloud instance metadata or probe
internal services from inside our own network.
"""

from __future__ import annotations

import base64
import hashlib
import io

import pytest
from PIL import Image
from starlette.datastructures import FormData, UploadFile

from tagverify.analyze import intake


def png(width: int = 40, height: int = 40, colour: str = "red") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(buffer, format="PNG")
    return buffer.getvalue()


# ------------------------------------------------------------------ SSRF


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",        # loopback
        "::1",              # loopback v6
        "169.254.169.254",  # cloud instance metadata — the classic target
        "10.0.0.1",         # private
        "172.16.0.5",       # private
        "192.168.1.1",      # private
        "100.64.0.1",       # carrier-grade NAT
        "0.0.0.0",          # unspecified
        "224.0.0.1",        # multicast
        "fd00::1",          # unique local v6
        "fe80::1",          # link-local v6
        "::ffff:127.0.0.1",  # IPv4-mapped loopback — must be judged as the v4 address
        "::ffff:169.254.169.254",
        "not-an-ip",        # unrecognised: refuse
    ],
)
def test_private_addresses_are_refused(address: str) -> None:
    assert intake._is_public(address) is False


@pytest.mark.parametrize(
    "address", ["93.184.216.34", "8.8.8.8", "2606:2800:220:1:248:1893:25c8:1946"]
)
def test_public_addresses_are_allowed(address: str) -> None:
    assert intake._is_public(address) is True


async def test_loopback_url_is_rejected() -> None:
    with pytest.raises(intake.ImageIntakeError) as caught:
        await intake._assert_public_url("http://127.0.0.1:8080/x.jpg")
    assert caught.value.code == "INVALID_IMAGE_URL"


async def test_metadata_url_is_rejected() -> None:
    with pytest.raises(intake.ImageIntakeError) as caught:
        await intake._assert_public_url("http://169.254.169.254/latest/meta-data/")
    assert caught.value.code == "INVALID_IMAGE_URL"


@pytest.mark.parametrize("url", ["file:///etc/passwd", "gopher://x/", "ftp://example.com/x.jpg"])
async def test_non_http_schemes_are_rejected(url: str) -> None:
    with pytest.raises(intake.ImageIntakeError) as caught:
        await intake._assert_public_url(url)
    assert caught.value.code == "INVALID_IMAGE_URL"


# ------------------------------------------------------------------ tags


def test_tags_from_a_comma_string() -> None:
    assert intake.normalise_tags("alcohol, vaping ,, alcohol") == ["alcohol", "vaping"]


def test_tags_from_a_list_are_deduped_in_order() -> None:
    assert intake.normalise_tags(["b", "a", "b"]) == ["b", "a"]


@pytest.mark.parametrize("value", [[], "", "   ,  ", None, 42])
def test_no_tags_is_an_error(value: object) -> None:
    with pytest.raises(intake.ImageIntakeError) as caught:
        intake.normalise_tags(value)
    assert caught.value.code == "NO_TAGS"


# ----------------------------------------------------------------- bytes


def test_hash_is_over_the_original_bytes() -> None:
    """
    Two callers sending the same file must land on the same cache key, whether or not our
    re-encoding step happened to be byte-deterministic for them.
    """
    data = png()
    assert intake.build(data, ["alcohol"]).hash == hashlib.sha256(data).hexdigest()


def test_empty_image_is_rejected() -> None:
    with pytest.raises(intake.ImageIntakeError) as caught:
        intake.build(b"", ["alcohol"])
    assert caught.value.code == "INVALID_IMAGE"


def test_non_image_bytes_are_rejected() -> None:
    with pytest.raises(intake.ImageIntakeError) as caught:
        intake.build(b"this is not an image, it is prose" * 20, ["alcohol"])
    assert caught.value.code == "INVALID_IMAGE"


def test_oversized_image_is_rejected() -> None:
    with pytest.raises(intake.ImageIntakeError) as caught:
        intake.build(b"\x89PNG" + b"\x00" * (intake.MAX_IMAGE_BYTES + 1), ["alcohol"])
    assert caught.value.code == "IMAGE_TOO_LARGE"


def test_large_image_is_downscaled_before_sending() -> None:
    """API callers who skip the client-side downscale get it done for them."""
    result = intake.build(png(2400, 1600), ["alcohol"])
    decoded = Image.open(io.BytesIO(base64.b64decode(result.frames[0].base64)))
    assert max(decoded.size) == intake.MAX_EDGE
    # but the hash and reported size still describe what was actually submitted
    assert result.bytes == len(png(2400, 1600))


def test_small_image_is_passed_through_untouched() -> None:
    data = png(100, 80)
    result = intake.build(data, ["alcohol"])
    assert base64.b64decode(result.frames[0].base64) == data


# ------------------------------------------------------------------ JSON


async def test_json_base64_intake() -> None:
    data = png()
    result = await intake.from_json(
        {"tags": ["alcohol"], "image_base64": base64.b64encode(data).decode()}
    )
    assert result.hash == hashlib.sha256(data).hexdigest()


async def test_json_accepts_a_data_url() -> None:
    data = png()
    payload = "data:image/png;base64," + base64.b64encode(data).decode()
    result = await intake.from_json({"tags": ["alcohol"], "image_base64": payload})
    assert result.hash == hashlib.sha256(data).hexdigest()


async def test_json_with_no_image_is_an_error() -> None:
    with pytest.raises(intake.ImageIntakeError) as caught:
        await intake.from_json({"tags": ["alcohol"]})
    assert caught.value.code == "NO_IMAGE"


async def test_invalid_base64_is_an_error() -> None:
    with pytest.raises(intake.ImageIntakeError) as caught:
        await intake.from_json({"tags": ["alcohol"], "image_base64": "not base64 !!!"})
    assert caught.value.code == "INVALID_IMAGE"


# ------------------------------------------------- finding the upload in a form


def form_with(**parts: object) -> FormData:
    """A FormData carrying the given parts; bytes become file parts."""
    items: list[tuple[str, object]] = []
    for name, value in parts.items():
        if isinstance(value, bytes):
            items.append(
                (name, UploadFile(filename=f"{name}.jpg", file=io.BytesIO(value)))
            )
        else:
            items.append((name, value))
    return FormData(items)


def test_the_documented_field_names_are_found() -> None:
    for name in ("media", "image"):
        found = intake.upload_from_form(form_with(**{name: png(), "tags": "alcohol"}))
        assert found is not None, name


def test_a_file_under_an_unexpected_name_is_still_found() -> None:
    """
    THE regression, and the reason this helper exists.

    A template renamed its input to `media` while the running server still read `image`. The
    request then looked file-less, and the user was told to "choose a creative" — the one
    thing they had already done. Depending on a string agreed in two separate files turns a
    rename into a lie; accepting any file part turns it into a non-event.
    """
    found = intake.upload_from_form(form_with(creative=png(), tags="alcohol"))
    assert found is not None


def test_media_wins_when_several_file_parts_are_present() -> None:
    """The documented name is preferred; the fallback is a safety net, not a coin toss."""
    form = form_with(other=png(), media=png(), tags="alcohol")
    found = intake.upload_from_form(form)
    assert isinstance(found, UploadFile)
    assert found.filename == "media.jpg"


def test_an_empty_file_input_is_not_an_upload() -> None:
    """
    A file input submitted with nothing selected still sends a part, with an empty filename.
    Accepting it would trade a clear "nothing attached" for a zero-byte decode failure.
    """
    form = FormData([("media", UploadFile(filename="", file=io.BytesIO(b"")))])
    assert intake.upload_from_form(form) is None


def test_a_form_with_no_file_returns_none() -> None:
    assert intake.upload_from_form(form_with(tags="alcohol")) is None


def test_field_names_are_reported_without_their_values() -> None:
    """Error messages name the fields that arrived — never their contents."""
    names = intake.form_field_names(form_with(media=png(), tags="alcohol"))
    assert "media" in names
    assert "tags" in names

    assert intake.form_field_names(form_with()) == "none"
