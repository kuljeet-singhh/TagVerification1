import { NextResponse } from "next/server";

/**
 * Typed error codes so callers can branch on `error` instead of regex-matching
 * prose.
 *
 * UNKNOWN_TAG deserves a note: it is an ERROR, never a silent skip. If a caller
 * misspells "alcohol" we must not return "no alcohol found" — "we didn't check"
 * and "we checked and it's clean" mean opposite things, and conflating them in a
 * compliance tool is how an illegal creative reaches a screen.
 */
export type ApiErrorCode =
  | "INVALID_KEY"
  | "MISSING_KEY"
  | "RATE_LIMITED"
  | "UNKNOWN_TAG"
  | "NO_TAGS"
  | "NO_IMAGE"
  | "IMAGE_TOO_LARGE"
  | "INVALID_IMAGE"
  | "INVALID_IMAGE_URL"
  | "BAD_REQUEST"
  | "INFERENCE_WARMING"
  | "INFERENCE_FAILED"
  | "INTERNAL";

const STATUS: Record<ApiErrorCode, number> = {
  MISSING_KEY: 401,
  INVALID_KEY: 401,
  RATE_LIMITED: 429,
  UNKNOWN_TAG: 400,
  NO_TAGS: 400,
  NO_IMAGE: 400,
  IMAGE_TOO_LARGE: 413,
  INVALID_IMAGE: 400,
  INVALID_IMAGE_URL: 400,
  BAD_REQUEST: 400,
  INFERENCE_WARMING: 503,
  INFERENCE_FAILED: 502,
  INTERNAL: 500,
};

export function apiError(
  code: ApiErrorCode,
  message: string,
  extra?: Record<string, unknown>,
) {
  const status = STATUS[code];
  const headers = new Headers();

  // Make retryability machine-readable, not something the caller has to infer.
  if (typeof extra?.retry_after === "number") {
    headers.set("Retry-After", String(extra.retry_after));
  }

  return NextResponse.json({ error: code, message, ...extra }, { status, headers });
}
