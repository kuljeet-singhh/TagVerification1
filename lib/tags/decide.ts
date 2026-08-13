import { db } from "@/lib/db";
import { tagThresholds, type TagThreshold } from "@/lib/db/schema";
import type { RawVerdict } from "@/lib/inference/client";

/**
 * Turn a raw score into a verdict, using thresholds from Postgres.
 *
 * WHY WE RE-DECIDE HERE INSTEAD OF TRUSTING THE SPACE
 * ---------------------------------------------------
 * The Space carries provisional thresholds in packs.json so its own test UI can
 * show something useful. But a threshold is only ever a comparison against a
 * number that has already been computed — so applying it here, in the API tier,
 * costs nothing and buys a lot: calibration results and hand-tuning take effect
 * on the next request with no Space redeploy and no model reload.
 *
 * The prompts cannot work this way (the Space must encode them into embeddings
 * at startup), which is exactly why prompts live in git and thresholds live in
 * the database.
 */

export type Verdict = {
  tag: string;
  /** null = uncertain; the caller should escalate to a vision LLM. */
  present: boolean | null;
  score: number;
  confidence: "high" | "medium" | "low";
  decidedBy: "siglip" | "sigmoid_floor";
  /** false when this tag's thresholds have never been calibrated against
   * labelled data. Surfaced to callers on purpose — an uncalibrated verdict is
   * a guess and should not be presented as a measurement. */
  calibrated: boolean;
  evidence: {
    topPhrase: string;
    crop: number[];
    sigmoid: number;
  };
};

/** Used when a tag has no row yet. Matches the defaults in packs.json. */
const FALLBACK: Pick<
  TagThreshold,
  "thresholdLow" | "thresholdHigh" | "sigmoidFloor" | "calibrated" | "packsVersionSeen"
> = {
  thresholdLow: 0.3,
  thresholdHigh: 0.55,
  sigmoidFloor: 0.005,
  calibrated: false,
  packsVersionSeen: null,
};

export async function loadThresholds(): Promise<Map<string, TagThreshold>> {
  const rows = await db.select().from(tagThresholds);
  return new Map(rows.map((row) => [row.slug, row]));
}

/**
 * Memoised thresholds for the /analyze hot path.
 *
 * Thresholds change only when calibration runs or someone edits them in the
 * admin UI — rare, and never mid-burst. Each Neon HTTP query is its own round
 * trip (~560-800ms cross-region), so re-reading 20 small rows on every request
 * is the single most wasteful thing this route could do.
 *
 * The 30s TTL is the lag between saving a threshold and it taking effect. That
 * is still dramatically better than the alternative these numbers replace: a
 * Space redeploy and model reload.
 */
const TTL_MS = 30_000;
let cache: { at: number; value: Map<string, TagThreshold> } | null = null;

export async function cachedThresholds(): Promise<Map<string, TagThreshold>> {
  if (cache && Date.now() - cache.at < TTL_MS) return cache.value;
  const value = await loadThresholds();
  cache = { at: Date.now(), value };
  return value;
}

/** Call after writing thresholds so admin edits are visible immediately. */
export function invalidateThresholdCache(): void {
  cache = null;
}

function confidenceOf(
  score: number,
  low: number,
  high: number,
  band: "present" | "absent" | "uncertain",
): "high" | "medium" | "low" {
  if (band === "uncertain") return "low";
  const margin = band === "present" ? score - high : low - score;
  if (margin >= 0.2) return "high";
  if (margin >= 0.08) return "medium";
  return "low";
}

export function decide(
  raw: RawVerdict,
  threshold: TagThreshold | undefined,
  /** The pack fingerprint the live inference service is actually running. */
  livePacksVersion?: string,
): Verdict {
  const t = threshold ?? FALLBACK;

  // A threshold is only meaningful for the prompts it was measured against.
  // Editing a pack changes the scores the model produces, so a threshold
  // calibrated against an older pack no longer describes anything — and this
  // drifts silently: calibrate, apply, forget to redeploy the Space, and stale
  // cutoffs get applied to different numbers with no error anywhere.
  //
  // We keep using the threshold (it is still the best guess available) but stop
  // claiming it is calibrated, so `calibrated: false` reaches the caller and the
  // admin UI shows the tag needs re-running. This was a real incident during
  // development, not a hypothetical.
  const stale =
    Boolean(livePacksVersion) &&
    Boolean(t.packsVersionSeen) &&
    t.packsVersionSeen !== livePacksVersion;
  const calibrated = t.calibrated && !stale;

  let band: "present" | "absent" | "uncertain";
  let decidedBy: "siglip" | "sigmoid_floor";

  // Order matters. The sigmoid floor is a VETO and is checked first: softmax
  // must sum to 1, so an image containing nothing relevant can still hand a
  // large share to a positive prompt purely by beating equally-irrelevant
  // options. Without this check a photo of a mountain can read as "alcohol".
  if (raw.sigmoid < t.sigmoidFloor) {
    band = "absent";
    decidedBy = "sigmoid_floor";
  } else if (raw.score >= t.thresholdHigh) {
    band = "present";
    decidedBy = "siglip";
  } else if (raw.score <= t.thresholdLow) {
    band = "absent";
    decidedBy = "siglip";
  } else {
    band = "uncertain";
    decidedBy = "siglip";
  }

  return {
    tag: raw.tag,
    present: band === "uncertain" ? null : band === "present",
    score: raw.score,
    confidence: confidenceOf(raw.score, t.thresholdLow, t.thresholdHigh, band),
    decidedBy,
    calibrated,
    evidence: {
      topPhrase: raw.top_phrase,
      crop: raw.crop,
      sigmoid: raw.sigmoid,
    },
  };
}

export function decideAll(
  results: RawVerdict[],
  thresholds: Map<string, TagThreshold>,
  livePacksVersion?: string,
): Verdict[] {
  return results.map((raw) =>
    decide(raw, thresholds.get(raw.tag), livePacksVersion),
  );
}
