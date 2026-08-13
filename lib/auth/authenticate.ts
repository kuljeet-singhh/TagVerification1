import { sql } from "drizzle-orm";

import { db } from "@/lib/db";
import type { ApiKey } from "@/lib/db/schema";

/**
 * Authenticate a key AND charge its rate limit in ONE database round trip.
 *
 * WHY THIS EXISTS
 * ---------------
 * The Neon HTTP driver sends each query as a separate HTTPS request, so latency
 * is (number of queries x round-trip time). Measured against a us-east-2
 * database from South Asia, one round trip is ~560-800ms — meaning a request
 * doing four sequential queries spends ~3s in transit before any real work.
 *
 * Verifying the key and incrementing its counter used to be two of those trips,
 * even though neither needs the other's result to be *sent*. A single CTE does
 * both: `k` finds the key, `bump` increments the counter only if `k` matched,
 * and the final select returns both. Same semantics, half the latency.
 *
 * This still matters after the region fix (see docs/DEPLOY.md — Vercel must be
 * deployed in the same region as Neon). It just stops being catastrophic.
 */

export type AuthOutcome =
  | { ok: true; key: ApiKey; count: number; limit: number; resetAfter: number }
  | { ok: false; reason: "invalid" };

async function sha256Hex(input: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(input));
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

const KEY_PREFIX = "dooh_live_";

export async function authenticateAndCharge(
  presented: string | null,
): Promise<AuthOutcome> {
  if (!presented || !presented.startsWith(KEY_PREFIX)) return { ok: false, reason: "invalid" };

  const keyHash = await sha256Hex(presented.trim());
  const now = new Date();
  const windowStart = new Date(Math.floor(now.getTime() / 60_000) * 60_000);

  const result = await db.execute(sql`
    with k as (
      select id, name, key_hash, key_prefix, rate_limit_per_min,
             revoked_at, last_used_at, created_at
      from api_keys
      where key_hash = ${keyHash} and revoked_at is null
    ),
    bump as (
      insert into usage_counters (api_key_id, window_start, count)
      select k.id, ${windowStart}, 1 from k
      on conflict (api_key_id, window_start)
        do update set count = usage_counters.count + 1
      returning api_key_id, count
    ),
    touched as (
      update api_keys set last_used_at = ${now}
      where id in (select id from k)
      returning id
    )
    select k.id, k.name, k.key_hash, k.key_prefix, k.rate_limit_per_min,
           k.revoked_at, k.last_used_at, k.created_at,
           coalesce(bump.count, 1) as count
    from k left join bump on bump.api_key_id = k.id
  `);

  const rows = (result as unknown as { rows?: Record<string, unknown>[] }).rows ?? [];
  const row = rows[0];
  if (!row) return { ok: false, reason: "invalid" };

  const key: ApiKey = {
    id: String(row.id),
    name: String(row.name),
    keyHash: String(row.key_hash),
    keyPrefix: String(row.key_prefix),
    rateLimitPerMin: Number(row.rate_limit_per_min),
    revokedAt: row.revoked_at ? new Date(String(row.revoked_at)) : null,
    lastUsedAt: row.last_used_at ? new Date(String(row.last_used_at)) : null,
    createdAt: new Date(String(row.created_at)),
  };

  return {
    ok: true,
    key,
    count: Number(row.count),
    limit: key.rateLimitPerMin,
    resetAfter: Math.max(
      1,
      Math.ceil((windowStart.getTime() + 60_000 - now.getTime()) / 1000),
    ),
  };
}
