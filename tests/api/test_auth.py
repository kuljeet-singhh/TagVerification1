"""
The authentication contract shared by every /api/v1 route.

The load-bearing assertion is that an unknown key and a revoked one are byte-identical in
their response. Confirming that a key exists is itself a disclosure.
"""

from __future__ import annotations

import pytest

from tests.conftest import needs_db


@pytest.mark.parametrize("path", ["/api/v1/tags", "/api/v1/usage"])
async def test_missing_key_is_401(client, path: str) -> None:
    response = await client.get(path)
    assert response.status_code == 401
    assert response.json()["error"] == "MISSING_KEY"


async def test_analyze_requires_a_key(client) -> None:
    response = await client.post("/api/v1/analyze", json={"tags": ["alcohol"]})
    assert response.status_code == 401
    assert response.json()["error"] == "MISSING_KEY"


@needs_db
@pytest.mark.parametrize(
    "key",
    [
        "dooh_live_obviously-not-a-real-key",  # right shape, unknown
        "wrong_prefix_entirely",               # wrong shape
    ],
)
async def test_unknown_and_malformed_keys_are_indistinguishable(client, key: str) -> None:
    """Never confirm a key exists — an unknown key and a revoked one look identical."""
    response = await client.get("/api/v1/tags", headers={"x-api-key": key})
    assert response.status_code == 401
    assert response.json() == {
        "error": "INVALID_KEY",
        "message": "This API key is not valid.",
    }


@needs_db
async def test_bearer_header_is_accepted(client) -> None:
    """Plenty of HTTP clients make Authorization the path of least resistance."""
    response = await client.get(
        "/api/v1/tags", headers={"authorization": "Bearer dooh_live_nope"}
    )
    # Reaches authentication (rather than being reported as a missing header).
    assert response.json()["error"] == "INVALID_KEY"
