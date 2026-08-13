import { eq } from "drizzle-orm";

import { db } from "@/lib/db";
import { apiKeys } from "@/lib/db/schema";

/**
 * API key issue / verify.
 *
 * The key is never stored. We keep sha256(key) and look up by that, so a
 * database leak does not hand out working credentials. Because lookup is an
 * exact match on an indexed hash, there is no string comparison to time-attack
 * and no need for a constant-time compare.
 *
 * sha256 (not bcrypt/argon2) is the right choice here specifically because the
 * key is 32 bytes of CSPRNG output, not a human-chosen password. There is no
 * dictionary to attack, so a slow KDF would only add latency to every single
 * API request.
 */

const KEY_PREFIX = "dooh_live_";
const SECRET_BYTES = 32;
const DISPLAY_CHARS = 8;

export type IssuedKey = {
  /** Shown to the user exactly once. Not recoverable afterwards. */
  plaintext: string;
  id: string;
  keyHash: string;
  keyPrefix: string;
};

async function sha256Hex(input: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(input));
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

function base64url(bytes: Uint8Array): string {
  return btoa(String.fromCharCode(...bytes))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}

/** Create a key. Caller is responsible for persisting and for showing the
 * plaintext once. */
export async function generateApiKey(): Promise<IssuedKey> {
  const secret = base64url(crypto.getRandomValues(new Uint8Array(SECRET_BYTES)));
  const plaintext = `${KEY_PREFIX}${secret}`;

  return {
    plaintext,
    id: crypto.randomUUID(),
    keyHash: await sha256Hex(plaintext),
    keyPrefix: secret.slice(0, DISPLAY_CHARS),
  };
}

/** Issue a key and persist it. Returns the plaintext — surface it once, then
 * discard it. */
export async function createApiKey(
  name: string,
  rateLimitPerMin = 60,
): Promise<IssuedKey> {
  const key = await generateApiKey();
  await db.insert(apiKeys).values({
    id: key.id,
    name,
    keyHash: key.keyHash,
    keyPrefix: key.keyPrefix,
    rateLimitPerMin,
  });
  return key;
}

/**
 * NOTE: request-time verification lives in lib/auth/authenticate.ts, not here.
 *
 * There is deliberately no `verifyApiKey()` in this file. An authenticate-only
 * helper that does not also charge the rate limit is a footgun: the next person
 * to add a route would reach for it and ship an unthrottled endpoint without
 * noticing. `authenticateAndCharge()` is the single door in, and it does both in
 * one query.
 */

export async function revokeApiKey(id: string): Promise<void> {
  await db.update(apiKeys).set({ revokedAt: new Date() }).where(eq(apiKeys.id, id));
}

/** For display: "dooh_live_a1b2c3d4…" */
export function maskedKey(keyPrefix: string): string {
  return `${KEY_PREFIX}${keyPrefix}…`;
}
