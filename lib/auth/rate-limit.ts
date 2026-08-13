import { sql } from "drizzle-orm";

import { db } from "@/lib/db";
import { usageCounters } from "@/lib/db/schema";

/**
 * Fixed-window rate limiting, one row per (key, minute).
 *
 * A single atomic upsert does the whole job: insert-or-increment, and the
 * RETURNING value tells us the new count. Doing it in one statement matters
 * because the HTTP Postgres driver has no multi-statement transactions — a
 * read-then-write would race between concurrent serverless invocations and let
 * callers slip past the limit.
 *
 * Fixed windows allow a burst across a boundary (up to 2x the limit in a
 * pathological case). A sliding window or token bucket would be tighter, but
 * this is abuse protection for a free-tier service, not billing enforcement.
 * Revisit if that changes.
 */

/**
 * The counter INCREMENT does not live here — it is folded into the
 * authentication query in lib/auth/authenticate.ts, so that verifying a key and
 * charging its quota cost one database round trip instead of two. This file
 * holds only the reads and maintenance.
 */

/** Calls made by a key today — powers GET /api/v1/usage. */
export async function usageToday(apiKeyId: string): Promise<number> {
  const since = new Date();
  since.setUTCHours(0, 0, 0, 0);

  const [row] = await db
    .select({ total: sql<number>`coalesce(sum(${usageCounters.count}), 0)::int` })
    .from(usageCounters)
    .where(
      sql`${usageCounters.apiKeyId} = ${apiKeyId} and ${usageCounters.windowStart} >= ${since}`,
    );

  return row?.total ?? 0;
}

/**
 * Drop counter rows older than a day. Nothing depends on them once the window
 * has passed; call from a cron or the admin UI so the table stays small enough
 * to fit Neon's 0.5GB free tier indefinitely.
 */
export async function pruneUsageCounters(): Promise<void> {
  await db.delete(usageCounters).where(sql`${usageCounters.windowStart} < now() - interval '1 day'`);
}
