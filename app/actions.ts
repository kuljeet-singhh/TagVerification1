"use server";

import { runAnalysis } from "@/lib/analyze/run";
import { InferenceError, InferenceWarmingError } from "@/lib/inference/client";
import type { Verdict } from "@/lib/tags/decide";

/**
 * Server Action behind the playground UI.
 *
 * Note this does NOT go through /api/v1/analyze. The public route exists to
 * authenticate and throttle *external* callers; the playground is already
 * server-side and same-origin, so routing through it would mean either shipping
 * an API key to the browser or issuing one to ourselves. Both callers share the
 * same pipeline (lib/analyze/run.ts), so behaviour cannot drift between them.
 */

const MAX_BYTES = 4 * 1024 * 1024;

export type PlaygroundResult =
  | {
      ok: true;
      verdicts: Verdict[];
      cached: boolean;
      latencyMs: number;
      model: string;
      packsVersion: string;
    }
  | { ok: false; message: string; retryable?: boolean };

async function sha256Hex(bytes: Uint8Array): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", bytes as unknown as ArrayBuffer);
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

function toBase64(bytes: Uint8Array): string {
  let binary = "";
  const CHUNK = 0x8000;
  for (let i = 0; i < bytes.length; i += CHUNK) {
    binary += String.fromCharCode(...bytes.subarray(i, i + CHUNK));
  }
  return btoa(binary);
}

export async function analyzeAction(formData: FormData): Promise<PlaygroundResult> {
  const file = formData.get("image");
  const tags = formData.getAll("tags").map(String).filter(Boolean);

  if (!(file instanceof File) || file.size === 0) {
    return { ok: false, message: "Choose an image first." };
  }
  if (tags.length === 0) {
    return { ok: false, message: "Select at least one tag to verify." };
  }
  if (file.size > MAX_BYTES) {
    return {
      ok: false,
      message: `That image is ${Math.round(file.size / 1024)}KB. The browser normally downscales before upload — try a smaller file.`,
    };
  }

  const bytes = new Uint8Array(await file.arrayBuffer());

  try {
    const outcome = await runAnalysis({
      imageBase64: toBase64(bytes),
      imageHash: await sha256Hex(bytes),
      imageBytes: bytes.length,
      tags,
      apiKeyId: null, // internal playground traffic, not billed to a key
    });

    if (!outcome.ok) {
      return { ok: false, message: `Unknown tag(s): ${outcome.unknown.join(", ")}` };
    }

    return {
      ok: true,
      verdicts: outcome.verdicts,
      cached: outcome.cached,
      latencyMs: outcome.latencyMs,
      model: outcome.model,
      packsVersion: outcome.packsVersion,
    };
  } catch (error) {
    if (error instanceof InferenceWarmingError) {
      return {
        ok: false,
        retryable: true,
        message:
          "The inference service is waking up (it sleeps after 48h idle). Give it a minute and try again.",
      };
    }
    if (error instanceof InferenceError) {
      return { ok: false, message: `Inference failed: ${error.message}` };
    }
    console.error("[analyzeAction]", error);
    return { ok: false, message: "Something went wrong. Check the server logs." };
  }
}
