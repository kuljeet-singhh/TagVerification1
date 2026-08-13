import { NextResponse } from "next/server";

import { apiError } from "@/lib/api/errors";
import { guard } from "@/lib/api/guard";
import {
  InferenceError,
  InferenceWarmingError,
  tagCatalog,
} from "@/lib/inference/client";
import { cachedThresholds as loadThresholds } from "@/lib/tags/decide";

export const runtime = "nodejs";

/**
 * GET /api/v1/tags — the tag catalog.
 *
 * Exposes slug, label, description and whether the tag has been calibrated.
 * Deliberately does NOT expose thresholds: those are tuning internals, and
 * publishing them would both invite gaming and make them awkward to change.
 *
 * `calibrated: false` is surfaced on purpose so an integrator can see which
 * tags are still running on guessed thresholds.
 */
export async function GET(request: Request) {
  const auth = await guard(request);
  if ("response" in auth) return auth.response;

  try {
    const [catalog, thresholds] = await Promise.all([tagCatalog(), loadThresholds()]);

    return NextResponse.json({
      packs_version: catalog.packs_version,
      count: catalog.tags.length,
      tags: catalog.tags.map((tag) => ({
        ...tag,
        calibrated: thresholds.get(tag.slug)?.calibrated ?? false,
      })),
    });
  } catch (error) {
    if (error instanceof InferenceWarmingError) {
      return apiError("INFERENCE_WARMING", "The inference service is starting up.", {
        retry_after: error.retryAfter,
      });
    }
    if (error instanceof InferenceError) {
      return apiError("INFERENCE_FAILED", error.message);
    }
    throw error;
  }
}
