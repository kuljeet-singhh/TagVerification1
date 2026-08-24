"""
The error envelope, and the refusals that produce it.

Every failure is a flat {"error": CODE, "message": ...} so a caller can branch on the code;
FastAPI's own {"detail": ...} must never leak through. See tagverify/errors.py.

test_unknown_tag_is_refused_not_skipped is the most important test in this directory: it
pins that "we didn't check" can never come back looking like "we checked and it's clean".
"""

from __future__ import annotations

import base64

import pytest

from tests.conftest import needs_db, needs_inference


@needs_db
@needs_inference
async def test_error_envelope_shape(client, api_key: str) -> None:
    """Every failure is {"error": CODE, "message": ...} so callers can branch on the code."""
    response = await client.post(
        "/api/v1/analyze", json={"tags": []}, headers={"x-api-key": api_key}
    )
    body = response.json()
    assert set(body) >= {"error", "message"}
    assert body["error"] == "NO_TAGS"
    assert response.status_code == 400
    # FastAPI's own {"detail": ...} shape must never leak through.
    assert "detail" not in body


@needs_db
async def test_unsupported_content_type(client, api_key: str) -> None:
    response = await client.post(
        "/api/v1/analyze",
        content=b"raw",
        headers={"x-api-key": api_key, "content-type": "text/plain"},
    )
    assert response.json()["error"] == "BAD_REQUEST"


@needs_db
async def test_malformed_json(client, api_key: str) -> None:
    response = await client.post(
        "/api/v1/analyze",
        content=b"{not json",
        headers={"x-api-key": api_key, "content-type": "application/json"},
    )
    assert response.json()["error"] == "BAD_REQUEST"


@needs_db
@needs_inference
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/x.jpg",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/x.jpg",
        "file:///etc/passwd",
    ],
)
async def test_ssrf_urls_are_refused(client, api_key: str, url: str) -> None:
    response = await client.post(
        "/api/v1/analyze",
        json={"tags": ["alcohol"], "image_url": url},
        headers={"x-api-key": api_key},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "INVALID_IMAGE_URL"


@needs_db
@needs_inference
async def test_unknown_tag_is_refused_not_skipped(client, api_key: str, beer_image: bytes) -> None:
    """
    The single most important error in the product. "We didn't check" must never come back
    looking like "we checked and it's clean".
    """
    response = await client.post(
        "/api/v1/analyze",
        json={
            "tags": ["alcohol", "alcohool"],
            "image_base64": base64.b64encode(beer_image).decode(),
        },
        headers={"x-api-key": api_key},
    )
    assert response.status_code == 400

    body = response.json()
    assert body["error"] == "UNKNOWN_TAG"
    assert body["unknown_tags"] == ["alcohool"]
    assert "alcohol" in body["known_tags"]
    # No partial results: the whole request is refused.
    assert "results" not in body
