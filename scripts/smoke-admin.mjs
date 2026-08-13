/**
 * Admin session tests.
 *
 *   npm run smoke:admin
 *
 * The admin pages can mint API keys and change the thresholds that decide
 * compliance verdicts, so the gate is worth testing directly rather than trusting
 * a click-through. Because the cookie is HMAC-signed with ADMIN_PASSWORD and this
 * script can read that from the environment, it can forge candidate cookies and
 * confirm each one is refused.
 */
import { createHmac } from "node:crypto";

const BASE = process.env.BASE_URL ?? "http://localhost:3000";
const PASSWORD = process.env.ADMIN_PASSWORD;

if (!PASSWORD) {
  console.error("ADMIN_PASSWORD not set — run via `npm run smoke:admin`");
  process.exit(1);
}

let passed = 0;
let failed = 0;

function check(name, condition, detail = "") {
  if (condition) {
    passed++;
    console.log(`  PASS  ${name}`);
  } else {
    failed++;
    console.log(`  FAIL  ${name} ${detail}`);
  }
}

const sign = (payload, key = PASSWORD) =>
  createHmac("sha256", key).update(payload).digest("hex");

async function fetchAdmin(cookie) {
  const res = await fetch(`${BASE}/admin`, {
    headers: cookie ? { cookie: `dooh_admin=${cookie}` } : {},
    redirect: "manual",
  });
  return res.text();
}

const isLocked = (html) => html.includes("Admin sign in");
const isUnlocked = (html) => html.includes("API keys") && html.includes("Decision thresholds");

console.log(`\ntesting admin gate at ${BASE}\n`);

check("no cookie is locked out", isLocked(await fetchAdmin(null)));

check(
  "cookie with a bad signature is rejected",
  isLocked(await fetchAdmin(`${Date.now()}.${"0".repeat(64)}`)),
);

check(
  "cookie signed with the wrong password is rejected",
  isLocked(
    await fetchAdmin(`${Date.now()}.${sign(String(Date.now()), "not-the-password")}`),
  ),
);

{
  // Correctly signed, but issued 8 days ago — past the 7-day max age.
  const old = String(Date.now() - 8 * 24 * 60 * 60 * 1000);
  check("correctly signed but expired cookie is rejected", isLocked(await fetchAdmin(`${old}.${sign(old)}`)));
}

{
  // Correctly signed but dated in the future — a clock-skew / replay attempt.
  const future = String(Date.now() + 60 * 60 * 1000);
  check("future-dated cookie is rejected", isLocked(await fetchAdmin(`${future}.${sign(future)}`)));
}

check("malformed cookie is rejected", isLocked(await fetchAdmin("garbage")));
check("cookie with no signature is rejected", isLocked(await fetchAdmin(String(Date.now()))));

{
  // The positive case: a genuinely valid session must actually work, otherwise
  // all of the above would "pass" simply because /admin is always locked.
  const now = String(Date.now());
  const html = await fetchAdmin(`${now}.${sign(now)}`);
  check("a validly signed current cookie IS admitted", isUnlocked(html), html.slice(0, 200));
}

console.log(`\n${passed} passed, ${failed} failed\n`);
process.exit(failed > 0 ? 1 : 0);
