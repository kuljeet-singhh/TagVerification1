import type { NextResponse } from "next/server";

import { authenticateAndCharge } from "@/lib/auth/authenticate";
import type { ApiKey } from "@/lib/db/schema";
import { apiError } from "./errors";

/**
 * Authenticate a request and charge it against the key's rate limit.
 *
 * Returns either `{ key }` or a ready-to-return error Response, so route
 * handlers stay a straight line:
 *
 *   const auth = await guard(request);
 *   if ("response" in auth) return auth.response;
 *
 * This is exactly one database round trip — see lib/auth/authenticate.ts for
 * why that is worth the CTE.
 */
export type GuardResult =
  | { key: ApiKey; rateLimit: { limit: number; remaining: number } }
  | { response: NextResponse };

export async function guard(request: Request): Promise<GuardResult> {
  const presented =
    request.headers.get("x-api-key") ??
    // Also accept `Authorization: Bearer <key>`, since plenty of HTTP clients
    // make that the path of least resistance.
    request.headers.get("authorization")?.replace(/^Bearer\s+/i, "") ??
    null;

  if (!presented) {
    return {
      response: apiError("MISSING_KEY", "Provide your API key in the x-api-key header."),
    };
  }

  const outcome = await authenticateAndCharge(presented);

  if (!outcome.ok) {
    // Same response for unknown and revoked — never confirm a key exists.
    return { response: apiError("INVALID_KEY", "This API key is not valid.") };
  }

  if (outcome.count > outcome.limit) {
    return {
      response: apiError(
        "RATE_LIMITED",
        `Rate limit of ${outcome.limit} requests/minute exceeded.`,
        { retry_after: outcome.resetAfter, limit: outcome.limit },
      ),
    };
  }

  return {
    key: outcome.key,
    rateLimit: {
      limit: outcome.limit,
      remaining: Math.max(0, outcome.limit - outcome.count),
    },
  };
}
