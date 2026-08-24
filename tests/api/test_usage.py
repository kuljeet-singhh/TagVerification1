"""
GET /api/v1/usage — strictly scoped to the key presented.

There is no aggregate view here; that lives behind /admin. The plaintext key must never come
back out of the database.
"""

from __future__ import annotations

from tests.conftest import needs_db


@needs_db
async def test_usage_is_scoped_to_the_presented_key(client, api_key: str) -> None:
    response = await client.get("/api/v1/usage", headers={"x-api-key": api_key})
    assert response.status_code == 200

    body = response.json()
    assert body["key"]["masked"].startswith("dooh_live_")
    assert body["key"]["masked"].endswith("…")
    # The plaintext must never come back out of the database.
    assert api_key not in response.text
    assert set(body["today"]) == {
        "since", "requests", "analyses", "cache_hits", "avg_latency_ms",
    }
