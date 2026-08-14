import { NextResponse } from "next/server";

import { db } from "@/lib/db";
import { InferenceWarmingError, inferenceHealth } from "@/lib/inference/client";

export const runtime = "nodejs";

/**
 * GET /api/v1/health — unauthenticated on purpose.
 *
 * This is what the keepalive cron hits, and monitoring that needs a credential
 * tends to stop being monitoring. It reports only whether dependencies are
 * reachable, never anything about traffic or keys.
 *
 * Always returns 200 with a body describing what is up, rather than failing —
 * "the Space is warming" is useful information, not an error.
 */
export async function GET() {
  const started = Date.now();

  const [database, inference] = await Promise.all([
    // Deliberately wrapped: `db` is a Proxy whose get trap calls connect(),
    // which THROWS SYNCHRONOUSLY when DATABASE_URL is unset — before any
    // promise exists, so a bare `.catch()` never attaches and the throw escapes
    // this handler as a blank 500. Reporting a missing DATABASE_URL is exactly
    // this endpoint's job, so it must not be the one thing that kills it.
    Promise.resolve()
      .then(() => db.execute("select 1"))
      .then(() => ({ ok: true as const }))
      .catch((error: Error) => ({ ok: false as const, error: error.message })),
    inferenceHealth()
      .then((health) => ({
        ok: true as const,
        warm: true,
        model: health.model,
        packs_version: health.packs_version,
        tags: health.tags.length,
        prompts: health.prompts,
      }))
      .catch((error: Error) => ({
        ok: false as const,
        // Distinguish "asleep, will wake" from "genuinely broken".
        warm: false,
        warming: error instanceof InferenceWarmingError,
        error: error.message,
      })),
  ]);

  return NextResponse.json({
    status: database.ok && inference.ok ? "ok" : "degraded",
    checked_in_ms: Date.now() - started,
    database,
    inference,
  });
}
