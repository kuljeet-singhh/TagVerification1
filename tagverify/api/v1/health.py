"""
GET /api/v1/health

UNAUTHENTICATED on purpose. Monitoring that needs a credential tends to stop being
monitoring, and the keepalive cron that stops the free Space sleeping has to be able to
reach it too.

ALWAYS returns 200, even when everything is down — the status is in the body. A health check
that 500s tells a load balancer to take the instance out of rotation, which is exactly wrong
when the thing that is unhealthy is a downstream dependency.
"""

from __future__ import annotations

import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from tagverify.config import settings
from tagverify.db.session import DatabaseNotConfigured, is_configured, session_scope
from tagverify.scoring import registry
from tagverify.scoring.client import InferenceWarming, inference_health
from tagverify.tags.decide import RULE_VERSION, cached_thresholds, decision_version

router = APIRouter()


async def _database() -> dict[str, object]:
    if not is_configured():
        return {"ok": False, "error": "DATABASE_URL is not set"}
    try:
        async with session_scope() as session:
            await session.execute(text("select 1"))
        return {"ok": True}
    except DatabaseNotConfigured as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - any failure is a reportable health fact
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


async def _inference() -> dict[str, object]:
    """
    The scorer that is ACTUALLY in the request path — not whichever one happens to be up.

    This used to check the Space unconditionally. On a box running a VLM that meant a green
    tick for a component doing no work, and `status: degraded` whenever a Space nothing
    depended on went to sleep. Monitoring that describes the wrong dependency is worse than
    none, because it gets believed.
    """
    scorer = registry.module()
    if scorer is None:
        return await _siglip()

    # A VLM has no health endpoint worth calling: there is no process of ours to be warm or
    # cold, and probing the provider on every monitoring hit would bill us to learn something
    # the next real request finds out anyway. What IS checkable, and what actually breaks, is
    # whether it is configured and whether the catalog it answers from is readable.
    if settings().scorer_target is None:
        return {
            "ok": False,
            "warm": False,
            "warming": False,
            "scorer": registry.describe(),
            "error": "the active scorer is not configured (missing API key or unknown SCORER)",
        }

    try:
        async with session_scope() as session:
            live = await scorer.health(session)
    except Exception as exc:  # noqa: BLE001 - any failure is a reportable health fact
        return {
            "ok": False,
            "warm": False,
            "warming": False,
            "scorer": registry.describe(),
            "error": f"{type(exc).__name__}: {exc}",
        }

    return {
        "ok": True,
        # Always warm: nothing is loaded at startup, which is the point. The failure mode the
        # Space had — a restart silently reverting the catalog — cannot occur when the prompt
        # is read per request.
        "warm": True,
        "scorer": registry.describe(),
        "model": live.model,
        "packs_version": live.packs_version,
        "tags": len(live.tags),
    }


async def _siglip() -> dict[str, object]:
    try:
        health = await inference_health()
    except InferenceWarming as exc:
        # Distinct from a hard failure: the Space is asleep or loading, and will recover on
        # its own. A dashboard should show this differently from "unreachable".
        return {"ok": False, "warm": False, "warming": True, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "warm": False, "warming": False, "error": str(exc)}

    return {
        "ok": True,
        "warm": True,
        "scorer": "siglip",
        "model": health.model,
        "packs_version": health.packs_version,
        "tags": len(health.tags),
        "prompts": health.prompts,
    }


async def _decision() -> dict[str, object]:
    """
    Which decision rule and thresholds this process is ACTUALLY applying.

    Reported because it was otherwise unanswerable from outside: a server started without
    `--reload` served a superseded banding rule for hours, every response looked normal, and
    the only way to find out was to read `ps` output. `rule` changing after a restart is the
    one-command confirmation that a code change took effect.

    `rule` is reported even when Postgres is down, because it is knowable without it and it is
    the half that answers that question. `version` needs the thresholds, so it degrades.

    THE SCORER RULE IS FOLDED IN HERE TOO, and it has to be: `analyze/run.py` stamps
    `decision_version(thresholds, registry.rule())` onto every verdict, so reporting the bare
    version here would publish a fingerprint this service never issues. A field whose whole
    job is answering "is my change live?" is worse than absent when it answers wrongly.
    """
    out: dict[str, object] = {"rule": RULE_VERSION}
    try:
        async with session_scope() as session:
            out["version"] = decision_version(
                await cached_thresholds(session), registry.rule()
            )
    except Exception as exc:  # noqa: BLE001 - any failure is a reportable health fact
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


@router.get("/health")
async def health() -> JSONResponse:
    started = time.monotonic()
    database = await _database()
    inference = await _inference()
    decision = await _decision()

    return JSONResponse(
        {
            "status": "ok" if database["ok"] and inference["ok"] else "degraded",
            "checked_in_ms": int((time.monotonic() - started) * 1000),
            "database": database,
            "inference": inference,
            # Not part of `status`: a stale rule is not a fault, it is a fact you have to be
            # able to see. Folding it into ok/degraded would page someone for a deploy.
            "decision": decision,
        }
    )
