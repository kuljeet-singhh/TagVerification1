"""
GET /api/v1/tags — the live catalog.

Thresholds are never published. Doing so would invite gaming and freeze them into the
public contract; decision_version exists so a caller can key a cache on the rule instead.
"""

from __future__ import annotations

from tests.conftest import needs_db, needs_inference


@needs_db
@needs_inference
async def test_tags_catalog(client, api_key: str) -> None:
    response = await client.get("/api/v1/tags", headers={"x-api-key": api_key})
    assert response.status_code == 200

    body = response.json()
    assert body["count"] == len(body["tags"]) == 20
    assert set(body["tags"][0]) == {"slug", "label", "description", "calibrated"}


@needs_db
@needs_inference
async def test_tags_never_leaks_thresholds(client, api_key: str) -> None:
    """Publishing them would invite gaming and make them part of the public contract."""
    text = (await client.get("/api/v1/tags", headers={"x-api-key": api_key})).text
    for leak in ("threshold_low", "threshold_high", "sigmoid_floor"):
        assert leak not in text
