/**
 * Apply inference/calibration.json to the database.
 *
 *   npm run calibrate:apply              # show what would change, then apply
 *   npm run calibrate:apply -- --dry-run # show only
 *
 * `calibrate.py` measures thresholds and writes JSON; this loads them into
 * Postgres, where the API tier reads them. The split keeps database credentials
 * out of the inference service (which deploys to a Hugging Face Space) and leaves
 * a reviewable artifact between measuring and changing live verdicts.
 */
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { eq } from "drizzle-orm";

import { db } from "@/lib/db";
import { evalImages, tagThresholds } from "@/lib/db/schema";

type TagResult = {
  slug: string;
  calibrated: boolean;
  reason: string;
  n_pos: number;
  n_neg: number;
  threshold_low?: number;
  threshold_high?: number;
  sigmoid_floor?: number;
  precision?: number;
  precision_lower?: number;
  recall?: number;
  escalation_rate?: number;
  images: { path: string; label: boolean; score: number; sigmoid: number }[];
};

type Report = {
  packs_version: string;
  model: string;
  target_precision: number;
  tags: TagResult[];
};

async function main() {
  const dryRun = process.argv.includes("--dry-run");
  const reportPath = resolve(process.cwd(), "inference/calibration.json");

  let report: Report;
  try {
    report = JSON.parse(readFileSync(reportPath, "utf8")) as Report;
  } catch {
    console.error(
      `Could not read ${reportPath}\n\n` +
        "Run the calibration first:\n" +
        "    cd inference && ./.venv/bin/python calibrate.py",
    );
    process.exit(1);
  }

  // The pack fingerprint the thresholds were measured against must match what is
  // deployed. If someone edited a prompt after calibrating, the numbers describe
  // scores the model no longer produces — applying them would be worse than
  // leaving the guesses in place.
  const packsPath = resolve(process.cwd(), "inference/packs.json");
  const currentVersion = createHash("sha256")
    .update(readFileSync(packsPath, "utf8"))
    .digest("hex")
    .slice(0, 12);

  if (currentVersion !== report.packs_version) {
    console.error(
      `Pack version mismatch.\n\n` +
        `  calibration.json measured against: ${report.packs_version}\n` +
        `  inference/packs.json is now:       ${currentVersion}\n\n` +
        "packs.json changed after calibration ran, so these thresholds describe\n" +
        "scores the model no longer produces. Re-run calibrate.py.",
    );
    process.exit(1);
  }

  const applicable = report.tags.filter((t) => t.calibrated);
  const skipped = report.tags.filter((t) => !t.calibrated);

  console.log(
    `\ncalibration.json — model ${report.model}, packs ${report.packs_version}, ` +
      `target ${(report.target_precision * 100).toFixed(0)}%\n`,
  );

  const existing = await db.select().from(tagThresholds);
  const before = new Map(existing.map((r) => [r.slug, r]));

  console.log(
    `${"tag".padEnd(22)} ${"low".padStart(15)} ${"high".padStart(15)} ${"precision".padStart(10)} ${"recall".padStart(8)}`,
  );
  console.log("-".repeat(76));

  for (const tag of applicable) {
    const prev = before.get(tag.slug);
    const lowChange = `${prev?.thresholdLow ?? "—"} → ${tag.threshold_low}`;
    const highChange = `${prev?.thresholdHigh ?? "—"} → ${tag.threshold_high}`;
    console.log(
      `${tag.slug.padEnd(22)} ${lowChange.padStart(15)} ${highChange.padStart(15)} ` +
        `${`${((tag.precision ?? 0) * 100).toFixed(0)}%`.padStart(10)} ` +
        `${`${((tag.recall ?? 0) * 100).toFixed(0)}%`.padStart(8)}`,
    );
  }

  if (skipped.length > 0) {
    console.log(`\nnot applied (${skipped.length}):`);
    for (const tag of skipped) {
      console.log(`  ${tag.slug.padEnd(22)} ${tag.reason}`);
    }
  }

  if (dryRun) {
    console.log("\n--dry-run: nothing written.");
    return;
  }
  if (applicable.length === 0) {
    console.log("\nNothing to apply — no tag reached its precision target.");
    console.log("Add more labelled near-miss negatives, or rewrite the prompt packs.");
    return;
  }

  for (const tag of applicable) {
    await db
      .update(tagThresholds)
      .set({
        thresholdLow: tag.threshold_low,
        thresholdHigh: tag.threshold_high,
        sigmoidFloor: tag.sigmoid_floor,
        calibrated: true,
        // Store the confidence LOWER bound, not the point estimate. It is the
        // number selection was judged against, and the one that survives being
        // quoted to a client.
        precision: tag.precision_lower,
        recall: tag.recall,
        packsVersionSeen: report.packs_version,
        updatedAt: new Date(),
        updatedBy: "calibrate",
      })
      .where(eq(tagThresholds.slug, tag.slug));
  }

  // Record the eval set so a future disagreement can be traced to the exact
  // images and labels a threshold was derived from. Hash only — no image bytes.
  let recorded = 0;
  for (const tag of report.tags) {
    for (const image of tag.images) {
      const hash = createHash("sha256").update(image.path).digest("hex").slice(0, 32);
      await db
        .insert(evalImages)
        .values({
          id: crypto.randomUUID(),
          imageHash: hash,
          storageRef: image.path,
          tagSlug: tag.slug,
          label: image.label,
          note: `score=${image.score} sigmoid=${image.sigmoid}`,
        })
        .onConflictDoNothing();
      recorded++;
    }
  }

  console.log(
    `\napplied ${applicable.length} calibrated tag(s); recorded ${recorded} eval image label(s).`,
  );
  console.log(
    "Thresholds take effect within 30s (the API memoises them). The API will stop\n" +
      "reporting these tags as uncalibrated.",
  );
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
