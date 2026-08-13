import { cookies } from "next/headers";

/**
 * Admin session for /admin.
 *
 * The admin pages can mint and revoke API keys and change thresholds that decide
 * compliance verdicts, so they cannot be left open — a public /admin would let
 * anyone issue themselves a key.
 *
 * This is a single shared password, not user accounts, which is proportionate for
 * an internal tool. The session cookie is HMAC-signed with the password itself,
 * so it cannot be forged without knowing it, and changing the password
 * invalidates every existing session for free.
 *
 * If this ever faces more than a handful of internal people, replace it with real
 * accounts rather than adding roles on top of a shared secret.
 */

const COOKIE = "dooh_admin";
const MAX_AGE_SECONDS = 7 * 24 * 60 * 60;

function secret(): string | null {
  const value = process.env.ADMIN_PASSWORD?.trim();
  return value ? value : null;
}

async function sign(payload: string, key: string): Promise<string> {
  const cryptoKey = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(key),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const mac = await crypto.subtle.sign(
    "HMAC",
    cryptoKey,
    new TextEncoder().encode(payload),
  );
  return Array.from(new Uint8Array(mac))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

/** Constant-time compare — a signature check is exactly where a timing leak
 * would matter. */
function safeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

export type AdminState = "ok" | "locked" | "unconfigured";

/**
 * "unconfigured" is reported distinctly from "locked" on purpose: without
 * ADMIN_PASSWORD set there is nothing to authenticate against, and silently
 * allowing access would be the dangerous failure mode. We refuse and say why.
 */
export async function adminState(): Promise<AdminState> {
  const key = secret();
  if (!key) return "unconfigured";

  const raw = (await cookies()).get(COOKIE)?.value;
  if (!raw) return "locked";

  const [issuedAt, signature] = raw.split(".");
  if (!issuedAt || !signature) return "locked";

  const age = Date.now() - Number(issuedAt);
  if (!Number.isFinite(age) || age < 0 || age > MAX_AGE_SECONDS * 1000) return "locked";

  return safeEqual(await sign(issuedAt, key), signature) ? "ok" : "locked";
}

export async function login(password: string): Promise<AdminState> {
  const key = secret();
  if (!key) return "unconfigured";
  if (!safeEqual(password, key)) return "locked";

  const issuedAt = String(Date.now());
  (await cookies()).set(COOKIE, `${issuedAt}.${await sign(issuedAt, key)}`, {
    httpOnly: true,
    sameSite: "lax",
    secure: process.env.NODE_ENV === "production",
    path: "/",
    maxAge: MAX_AGE_SECONDS,
  });
  return "ok";
}

export async function logout(): Promise<void> {
  (await cookies()).delete(COOKIE);
}
