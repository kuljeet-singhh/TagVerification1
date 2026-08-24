"""
GET /api/v1/health.

Unauthenticated and always 200, both on purpose: monitoring that needs a credential stops
being monitoring, and a 500 would tell a load balancer to pull the instance when the
unhealthy thing is a downstream dependency. Any non-200 here means the app itself is down.
"""

from __future__ import annotations


async def test_health_is_unauthenticated_and_always_200(client) -> None:
    """
    Monitoring that needs a credential stops being monitoring, and a health check that 500s
    tells a load balancer to pull the instance when the unhealthy thing is a dependency.
    """
    response = await client.get("/api/v1/health")
    assert response.status_code == 200

    body = response.json()
    assert body["status"] in ("ok", "degraded")
    assert "ok" in body["database"]
    assert "ok" in body["inference"]
