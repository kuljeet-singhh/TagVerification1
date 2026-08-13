import { findCached, recordAnalysis } from "@/lib/db/cache";
import { analyzeImage, cachedInferenceHealth } from "@/lib/inference/client";
import { cachedThresholds, decideAll, type Verdict } from "@/lib/tags/decide";

/**
 * The analyze pipeline, shared by the public API route and the playground UI.
 *
 * Both callers need identical behaviour — same cache, same thresholds, same audit
 * trail — and the only differences are authentication (the API charges a key, the
 * UI does not) and how the outcome is rendered. Keeping the pipeline here means a
 * fix to caching or threshold handling cannot apply to one path and not the other.
 *
 * Throws InferenceWarmingError / InferenceError; callers translate those into an
 * HTTP status or a UI message.
 */

export type AnalysisSuccess = {
  ok: true;
  requestId: string;
  imageHash: string;
  model: string;
  packsVersion: string;
  cached: boolean;
  latencyMs: number;
  verdicts: Verdict[];
  /** Tags whose thresholds have never been calibrated against labelled data. */
  uncalibrated: string[];
};

export type AnalysisRejected = {
  ok: false;
  code: "UNKNOWN_TAG";
  unknown: string[];
  known: string[];
};

export type AnalysisOutcome = AnalysisSuccess | AnalysisRejected;

export async function runAnalysis(input: {
  imageBase64: string;
  imageHash: string;
  imageBytes: number;
  tags: string[];
  /** null for the internal playground; a key id for public API traffic. */
  apiKeyId: string | null;
}): Promise<AnalysisOutcome> {
  const startedAt = Date.now();

  // Memoised for 60s. The pack fingerprint is part of the cache key and the tag
  // list gates validation, so both are needed before anything else happens.
  const health = await cachedInferenceHealth();

  const unknown = input.tags.filter((tag) => !health.tags.includes(tag));
  if (unknown.length > 0) {
    // Never silently drop an unknown tag: "we didn't check" must not be
    // presented as "we checked and it's clean".
    return { ok: false, code: "UNKNOWN_TAG", unknown, known: health.tags };
  }

  // Independent reads — issued together, because each Neon HTTP query is its own
  // round trip and serialising them doubles the latency floor for no reason.
  const [cached, thresholds] = await Promise.all([
    findCached(input.imageHash, input.tags, health.packs_version),
    cachedThresholds(),
  ]);

  const raw = cached
    ? cached.results
    : (await analyzeImage(input.imageBase64, input.tags)).results;

  const verdicts = decideAll(raw, thresholds, health.packs_version);
  const requestId = crypto.randomUUID();
  const latencyMs = Date.now() - startedAt;

  // Audit trail. Deliberately not awaited — a logging failure must not cost the
  // caller a result they have already waited for.
  void recordAnalysis({
    requestId,
    apiKeyId: input.apiKeyId,
    imageHash: input.imageHash,
    tags: input.tags,
    results: raw,
    packsVersion: health.packs_version,
    latencyMs,
    cached: Boolean(cached),
  }).catch(() => {});

  return {
    ok: true,
    requestId,
    imageHash: input.imageHash,
    model: health.model,
    packsVersion: health.packs_version,
    cached: Boolean(cached),
    latencyMs,
    verdicts,
    uncalibrated: verdicts.filter((v) => !v.calibrated).map((v) => v.tag),
  };
}
