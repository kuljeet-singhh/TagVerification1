import { NextResponse } from "next/server";
import { and, eq, gte, sql } from "drizzle-orm";

import { guard } from "@/lib/api/guard";
import { db } from "@/lib/db";
import { analyses } from "@/lib/db/schema";
import { usageToday } from "@/lib/auth/rate-limit";
import { maskedKey } from "@/lib/auth/keys";

export const runtime = "nodejs";

/**
 * GET /api/v1/usage — what this key has done today.
 *
 * Scoped strictly to the presented key: a caller can never see another key's
 * traffic. `cache_hits` is worth exposing because cached calls are effectively
 * free, so an integrator can see the benefit of not re-uploading identical
 * creatives.
 */
export async function GET(request: Request) {
  const auth = await guard(request);
  if ("response" in auth) return auth.response;

  const since = new Date();
  since.setUTCHours(0, 0, 0, 0);

  const [calls, [stats]] = await Promise.all([
    usageToday(auth.key.id),
    db
      .select({
        analyses: sql<number>`count(*)::int`,
        cacheHits: sql<number>`count(*) filter (where ${analyses.cached})::int`,
        avgLatency: sql<number>`coalesce(round(avg(${analyses.latencyMs})), 0)::int`,
      })
      .from(analyses)
      .where(and(eq(analyses.apiKeyId, auth.key.id), gte(analyses.createdAt, since))),
  ]);

  return NextResponse.json({
    key: {
      name: auth.key.name,
      masked: maskedKey(auth.key.keyPrefix),
      rate_limit_per_min: auth.key.rateLimitPerMin,
    },
    today: {
      since: since.toISOString(),
      requests: calls,
      analyses: stats?.analyses ?? 0,
      cache_hits: stats?.cacheHits ?? 0,
      avg_latency_ms: stats?.avgLatency ?? 0,
    },
    rate_limit: {
      limit: auth.rateLimit.limit,
      remaining: auth.rateLimit.remaining,
    },
  });
}
