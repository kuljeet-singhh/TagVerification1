import { and, desc, eq, sql } from "drizzle-orm";

import { db } from "@/lib/db";
import { analyses } from "@/lib/db/schema";
import type { RawVerdict } from "@/lib/inference/client";

/**
 * Result cache, keyed on (image bytes, tag set, packs version).
 *
 * The cache stores RAW scores, not verdicts. That distinction is the whole
 * point: scores are the expensive part (a model forward pass) and never change
 * for the same image and prompts, while verdicts are a cheap comparison against
 * thresholds that we expect to retune often. So we cache the expensive half and
 * re-decide on every read — meaning a threshold change takes effect immediately,
 * even for images already in the cache.
 *
 * packsVersion is part of the key because editing a prompt changes the scores.
 * Without it, a pack rewrite would serve stale numbers forever.
 */

export type CachedAnalysis = {
  requestId: string;
  results: RawVerdict[];
  packsVersion: string | null;
};

function tagKey(tags: string[]): string[] {
  return [...tags].sort();
}

export async function findCached(
  imageHash: string,
  tags: string[],
  packsVersion: string,
): Promise<CachedAnalysis | null> {
  const wanted = tagKey(tags);

  const [row] = await db
    .select({
      requestId: analyses.requestId,
      results: analyses.results,
      packsVersion: analyses.packsVersion,
    })
    .from(analyses)
    .where(
      and(
        eq(analyses.imageHash, imageHash),
        eq(analyses.packsVersion, packsVersion),
        // Compare as sorted JSON arrays so tag order doesn't fragment the cache.
        sql`(select coalesce(jsonb_agg(t order by t), '[]'::jsonb)
             from jsonb_array_elements_text(${analyses.tagsRequested}) as t)
            = ${JSON.stringify(wanted)}::jsonb`,
      ),
    )
    .orderBy(desc(analyses.createdAt))
    .limit(1);

  if (!row) return null;
  return {
    requestId: row.requestId,
    results: row.results as RawVerdict[],
    packsVersion: row.packsVersion,
  };
}

export async function recordAnalysis(entry: {
  requestId: string;
  apiKeyId: string | null;
  imageHash: string;
  tags: string[];
  results: RawVerdict[];
  packsVersion: string;
  latencyMs: number;
  cached: boolean;
}): Promise<void> {
  await db.insert(analyses).values({
    requestId: entry.requestId,
    apiKeyId: entry.apiKeyId,
    imageHash: entry.imageHash,
    tagsRequested: tagKey(entry.tags),
    results: entry.results,
    packsVersion: entry.packsVersion,
    latencyMs: entry.latencyMs,
    cached: entry.cached,
  });
}
