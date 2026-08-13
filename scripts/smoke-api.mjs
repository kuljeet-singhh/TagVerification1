/**
 * End-to-end API test against a running dev server.
 *
 *   npm run smoke:api -- <api-key>
 *
 * Requires `npm run dev` and the inference service (`inference/app.py`) up.
 * Exercises the happy path, the cache, and every error branch — the error paths
 * matter as much as the successes, because a compliance API that returns a
 * cheerful "absent" for a misspelled tag is dangerous.
 */
import { readFileSync } from "node:fs";

const BASE = process.env.BASE_URL ?? "http://localhost:3000";
const KEY = process.argv[2];

if (!KEY) {
  console.error("usage: npm run smoke:api -- <api-key>");
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

async function post(body, headers = {}) {
  const res = await fetch(`${BASE}/api/v1/analyze`, {
    method: "POST",
    headers: { "x-api-key": KEY, ...headers },
    body,
  });
  return { status: res.status, json: await res.json().catch(() => null) };
}

function imageForm(path, tags) {
  const form = new FormData();
  form.append("image", new Blob([readFileSync(path)]), "creative.jpg");
  for (const tag of tags) form.append("tags", tag);
  return form;
}

const IMG = (name) => `inference/.testimages/${name}.jpg`;

console.log(`\ntesting ${BASE}\n`);

// ---------------------------------------------------------------- happy path
console.log("happy path");
{
  const { status, json } = await post(imageForm(IMG("Beer"), ["alcohol", "gym_fitness"]));
  check("200 OK", status === 200, `got ${status} ${JSON.stringify(json)}`);
  check("returns both tags", json?.results?.length === 2);

  const alcohol = json?.results?.find((r) => r.tag === "alcohol");
  const gym = json?.results?.find((r) => r.tag === "gym_fitness");

  check("beer -> alcohol present", alcohol?.present === true, `score=${alcohol?.score}`);
  check("beer -> gym absent", gym?.present === false, `score=${gym?.score}`);
  check("evidence has a phrase", Boolean(alcohol?.evidence?.top_phrase));
  check("evidence has a crop", alcohol?.evidence?.crop?.length === 4);
  check("reports uncalibrated tags", Array.isArray(json?.uncalibrated_tags));
  check("reports image hash", typeof json?.image_hash === "string");
  console.log(
    `        alcohol score=${alcohol?.score} phrase="${alcohol?.evidence?.top_phrase}" ` +
      `crop=[${alcohol?.evidence?.crop}]`,
  );
}

// -------------------------------------------------------------- hard negative
console.log("\nhard negatives (the cases that matter)");
for (const [image, tag, expected] of [
  ["Orange_juice", "alcohol", false],
  ["Coffee", "alcohol", false],
  ["Infant_formula", "protein_supplements", false],
  ["Mount_Everest", "alcohol", false],
  ["Whey_protein", "protein_supplements", true],
  ["Cigarette", "tobacco_smoking", true],
]) {
  const { json } = await post(imageForm(IMG(image), [tag]));
  const r = json?.results?.[0];
  check(
    `${image} -> ${tag} ${expected ? "present" : "absent"}`,
    r?.present === expected,
    `got present=${r?.present} score=${r?.score}`,
  );
}

// ---------------------------------------------------------------------- cache
console.log("\ncache");
{
  // The first call MUST be a genuine miss, or we would be timing two cache hits
  // against each other and calling it a cache test. Appending random trailing
  // bytes changes the sha256 while leaving the decoded image identical — JPEG
  // decoders ignore data after the end-of-image marker — so every run starts
  // cold regardless of what previous runs left in the database.
  const unique = new Uint8Array([
    ...readFileSync(IMG("Wine")),
    ...crypto.getRandomValues(new Uint8Array(16)),
  ]);
  const form = () => {
    const f = new FormData();
    f.append("image", new Blob([unique]), "creative.jpg");
    f.append("tags", "alcohol");
    return f;
  };

  const first = await post(form());
  const second = await post(form());

  check("first call is a cache miss", first.json?.cached === false, `got ${first.json?.cached}`);
  check("second identical call is served from cache", second.json?.cached === true);
  check(
    "cached call skips inference and is faster",
    second.json?.latency_ms < first.json?.latency_ms,
    `${first.json?.latency_ms}ms then ${second.json?.latency_ms}ms`,
  );
  check(
    "cached verdict matches uncached",
    first.json?.results?.[0]?.present === second.json?.results?.[0]?.present,
  );
  console.log(
    `        ${first.json?.latency_ms}ms uncached -> ${second.json?.latency_ms}ms cached`,
  );
}

// --------------------------------------------------------------- error paths
console.log("\nerror handling");
{
  const res = await fetch(`${BASE}/api/v1/analyze`, { method: "POST" });
  check("no key -> 401 MISSING_KEY", res.status === 401);
  check("code is MISSING_KEY", (await res.json())?.error === "MISSING_KEY");
}
{
  const res = await fetch(`${BASE}/api/v1/analyze`, {
    method: "POST",
    headers: { "x-api-key": "dooh_live_totally_wrong" },
    body: imageForm(IMG("Beer"), ["alcohol"]),
  });
  check("bad key -> 401", res.status === 401);
  check("code is INVALID_KEY", (await res.json())?.error === "INVALID_KEY");
}
{
  const { status, json } = await post(imageForm(IMG("Beer"), ["alcohol", "not_a_real_tag"]));
  check("unknown tag -> 400 (never a silent 'absent')", status === 400);
  check("code is UNKNOWN_TAG", json?.error === "UNKNOWN_TAG");
  check("echoes the unknown tag", json?.unknown_tags?.includes("not_a_real_tag"));
  check("echoes the valid list", Array.isArray(json?.known_tags));
}
{
  const { status, json } = await post(imageForm(IMG("Beer"), []));
  check("no tags -> 400 NO_TAGS", status === 400 && json?.error === "NO_TAGS");
}
{
  const { status, json } = await post(
    JSON.stringify({ tags: ["alcohol"] }),
    { "content-type": "application/json" },
  );
  check("no image -> 400 NO_IMAGE", status === 400 && json?.error === "NO_IMAGE");
}
{
  const { status, json } = await post(
    JSON.stringify({ image_base64: "!!!nope!!!", tags: ["alcohol"] }),
    { "content-type": "application/json" },
  );
  check("bad base64 -> 400 INVALID_IMAGE", status === 400 && json?.error === "INVALID_IMAGE");
}

// ------------------------------------------------------------------- SSRF
console.log("\nSSRF protection (image_url fetches on our behalf)");
for (const [label, url] of [
  ["loopback", "http://127.0.0.1:7860/"],
  ["cloud metadata", "http://169.254.169.254/latest/meta-data/"],
  ["private range", "http://10.0.0.1/x.jpg"],
  ["non-http scheme", "file:///etc/passwd"],
]) {
  const { status, json } = await post(
    JSON.stringify({ image_url: url, tags: ["alcohol"] }),
    { "content-type": "application/json" },
  );
  check(`refuses ${label}`, status === 400 && json?.error === "INVALID_IMAGE_URL", `got ${status} ${json?.error}`);
}

// ------------------------------------------------------------- other routes
console.log("\nother routes");
{
  const res = await fetch(`${BASE}/api/v1/tags`, { headers: { "x-api-key": KEY } });
  const json = await res.json();
  check("GET /tags 200", res.status === 200);
  check("catalog has 20 tags", json?.count === 20, `got ${json?.count}`);
  check("exposes calibrated flag", typeof json?.tags?.[0]?.calibrated === "boolean");
  check("does NOT leak thresholds", json?.tags?.[0]?.threshold_high === undefined);
}
{
  const res = await fetch(`${BASE}/api/v1/usage`, { headers: { "x-api-key": KEY } });
  const json = await res.json();
  check("GET /usage 200", res.status === 200);
  check("counts today's requests", (json?.today?.requests ?? 0) > 0, JSON.stringify(json?.today));
  check("masks the key", String(json?.key?.masked).endsWith("…"));
  console.log(
    `        requests=${json?.today?.requests} analyses=${json?.today?.analyses} ` +
      `cache_hits=${json?.today?.cache_hits} avg=${json?.today?.avg_latency_ms}ms`,
  );
}
{
  const res = await fetch(`${BASE}/api/v1/health`);
  const json = await res.json();
  check("GET /health 200 without a key", res.status === 200);
  check("reports db + inference", json?.database?.ok === true && json?.inference?.ok === true);
}

console.log(`\n${passed} passed, ${failed} failed\n`);
process.exit(failed > 0 ? 1 : 0);
