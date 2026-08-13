"use server";

import { revalidatePath } from "next/cache";
import { desc, eq } from "drizzle-orm";

import { adminState, login as doLogin, logout as doLogout } from "@/lib/auth/admin";
import { createApiKey, revokeApiKey } from "@/lib/auth/keys";
import { db } from "@/lib/db";
import { apiKeys, tagThresholds } from "@/lib/db/schema";
import { invalidateThresholdCache } from "@/lib/tags/decide";

/**
 * Every action here re-checks the admin session itself.
 *
 * A Server Action is a POST endpoint, reachable independently of the page that
 * renders the form. Guarding only the page would leave these callable by anyone
 * who knows the action id — so authorisation lives on the action, not the view.
 */
async function requireAdmin(): Promise<void> {
  if ((await adminState()) !== "ok") throw new Error("not authorised");
}

export async function loginAction(formData: FormData) {
  const state = await doLogin(String(formData.get("password") ?? ""));
  if (state === "ok") revalidatePath("/admin");
  return state;
}

export async function logoutAction() {
  await doLogout();
  revalidatePath("/admin");
}

export async function createKeyAction(formData: FormData) {
  await requireAdmin();

  const name = String(formData.get("name") ?? "").trim();
  const rateLimit = Number(formData.get("rateLimit") ?? 60);

  if (!name) return { ok: false as const, message: "Give the key a name." };
  if (!Number.isFinite(rateLimit) || rateLimit < 1 || rateLimit > 10_000) {
    return { ok: false as const, message: "Rate limit must be between 1 and 10000." };
  }

  const key = await createApiKey(name, rateLimit);
  revalidatePath("/admin");

  // The plaintext is returned exactly once, for display. It is stored only as a
  // sha256 hash, so this is the single moment it can ever be shown.
  return { ok: true as const, plaintext: key.plaintext, name };
}

export async function revokeKeyAction(id: string) {
  await requireAdmin();
  await revokeApiKey(id);
  revalidatePath("/admin");
  return { ok: true as const };
}

export async function listKeys() {
  await requireAdmin();
  return db.select().from(apiKeys).orderBy(desc(apiKeys.createdAt));
}

export async function saveThresholdAction(formData: FormData) {
  await requireAdmin();

  const slug = String(formData.get("slug") ?? "");
  const low = Number(formData.get("low"));
  const high = Number(formData.get("high"));
  const floor = Number(formData.get("floor"));

  if (!slug) return { ok: false as const, message: "Missing tag." };
  for (const [label, value] of [
    ["low", low],
    ["high", high],
    ["floor", floor],
  ] as const) {
    if (!Number.isFinite(value) || value < 0 || value > 1) {
      return { ok: false as const, message: `${label} must be between 0 and 1.` };
    }
  }
  if (low > high) {
    // Not pedantry: low > high would make the uncertain band inverted, so a score
    // could satisfy both "absent" and "present" and the ordering of the checks
    // would silently decide the verdict.
    return {
      ok: false as const,
      message: "Low threshold must not exceed the high threshold.",
    };
  }

  await db
    .update(tagThresholds)
    .set({
      thresholdLow: low,
      thresholdHigh: high,
      sigmoidFloor: floor,
      updatedAt: new Date(),
      updatedBy: "admin",
      // Hand-editing means these are no longer the calibrated numbers. Say so,
      // rather than letting the UI keep claiming a precision that was measured
      // against different thresholds.
      calibrated: false,
      precision: null,
      recall: null,
    })
    .where(eq(tagThresholds.slug, slug));

  // Thresholds are memoised for 30s on the hot path; clear it so an edit is
  // visible on the very next request.
  invalidateThresholdCache();
  revalidatePath("/admin");
  revalidatePath("/");

  return { ok: true as const, message: `${slug} updated.` };
}
