import { NextResponse } from "next/server";

import { apiError } from "@/lib/api/errors";
import { guard } from "@/lib/api/guard";
import { ImageIntakeError, readIntake } from "@/lib/api/image";
import { runAnalysis } from "@/lib/analyze/run";
import { InferenceError, InferenceWarmingError } from "@/lib/inference/client";

// node runtime: lib/api/image.ts uses node:dns and node:net for the SSRF check.
export const runtime = "nodejs";

/**
 * POST /api/v1/analyze
 *
 * Accepts multipart (`image`, `tags`) or JSON (`image_base64`|`image_url`, `tags`).
 * Returns a per-tag verdict with a confidence score and evidence.
 *
 * The pipeline itself lives in lib/analyze/run.ts, shared with the playground UI.
 * This handler only does HTTP: authenticate, parse, translate the outcome.
 */
export async function POST(request: Request) {
  const auth = await guard(request);
  if ("response" in auth) return auth.response;

  let intake;
  try {
    intake = await readIntake(request);
  } catch (error) {
    if (error instanceof ImageIntakeError) {
      return apiError(error.code, error.message);
    }
    throw error;
  }

  try {
    const outcome = await runAnalysis({
      imageBase64: intake.base64,
      imageHash: intake.hash,
      imageBytes: intake.bytes,
      tags: intake.tags,
      apiKeyId: auth.key.id,
    });

    if (!outcome.ok) {
      return apiError("UNKNOWN_TAG", `Unknown tag(s): ${outcome.unknown.join(", ")}`, {
        unknown_tags: outcome.unknown,
        known_tags: outcome.known,
      });
    }

    return NextResponse.json(
      {
        request_id: outcome.requestId,
        image_hash: outcome.imageHash,
        image_bytes: intake.bytes,
        model: outcome.model,
        packs_version: outcome.packsVersion,
        cached: outcome.cached,
        latency_ms: outcome.latencyMs,
        results: outcome.verdicts.map((v) => ({
          tag: v.tag,
          present: v.present,
          score: v.score,
          confidence: v.confidence,
          decided_by: v.decidedBy,
          calibrated: v.calibrated,
          evidence: {
            top_phrase: v.evidence.topPhrase,
            crop: v.evidence.crop,
            sigmoid: v.evidence.sigmoid,
          },
        })),
        // Stated explicitly rather than buried: an uncalibrated tag's verdict is
        // an educated guess, and the caller deserves to know which ones those are.
        ...(outcome.uncalibrated.length > 0
          ? { uncalibrated_tags: outcome.uncalibrated }
          : {}),
      },
      {
        headers: {
          "X-RateLimit-Limit": String(auth.rateLimit.limit),
          "X-RateLimit-Remaining": String(auth.rateLimit.remaining),
        },
      },
    );
  } catch (error) {
    if (error instanceof InferenceWarmingError) {
      return apiError(
        "INFERENCE_WARMING",
        "The inference service is starting up. Retry shortly.",
        { retry_after: error.retryAfter },
      );
    }
    if (error instanceof InferenceError) {
      return apiError("INFERENCE_FAILED", error.message);
    }
    console.error("[analyze] unexpected", error);
    return apiError("INTERNAL", "Unexpected error while analysing the image.");
  }
}
