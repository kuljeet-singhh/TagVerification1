/**
 * Seed tag_thresholds from inference/packs.json.
 *
 *   npm run seed
 *
 * Safe to re-run. Rows already marked `calibrated` are LEFT ALONE — re-seeding
 * must never quietly overwrite measured thresholds with the guesses that ship in
 * packs.json. New tags get inserted; uncalibrated existing rows get refreshed.
 *
 * DATABASE_URL comes from `--env-file=.env.local` (see the npm script), so plain
 * static imports are safe here — no dotenv-before-import ordering to get wrong.
 */
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { db } from "@/lib/db";
import { tagThresholds } from "@/lib/db/schema";

type Pack = {
  slug: string;
  label: string;
  threshold_low?: number;
  threshold_high?: number;
  sigmoid_floor?: number;
  escalate?: boolean;
};

type PacksFile = {
  defaults: {
    threshold_low: number;
    threshold_high: number;
    sigmoid_floor: number;
    escalate: boolean;
  };
  tags: Pack[];
};

async function main() {
  const packsPath = resolve(process.cwd(), "inference/packs.json");
  const rawFile = readFileSync(packsPath, "utf8");
  const packs = JSON.parse(rawFile) as PacksFile;

  // Must match detector.py: sha256 of the whole file, first 12 hex chars.
  const packsVersion = createHash("sha256").update(rawFile).digest("hex").slice(0, 12);

  const existing = await db.select().from(tagThresholds);
  const calibrated = new Set(existing.filter((r) => r.calibrated).map((r) => r.slug));

  let inserted = 0;
  let refreshed = 0;

  for (const pack of packs.tags) {
    if (calibrated.has(pack.slug)) {
      console.log(`  skip     ${pack.slug} (calibrated — not overwriting measured values)`);
      continue;
    }

    const values = {
      slug: pack.slug,
      thresholdLow: pack.threshold_low ?? packs.defaults.threshold_low,
      thresholdHigh: pack.threshold_high ?? packs.defaults.threshold_high,
      sigmoidFloor: pack.sigmoid_floor ?? packs.defaults.sigmoid_floor,
      escalate: pack.escalate ?? packs.defaults.escalate,
      calibrated: false,
      packsVersionSeen: packsVersion,
      updatedAt: new Date(),
      updatedBy: "seed",
    };

    const wasThere = existing.some((r) => r.slug === pack.slug);
    await db
      .insert(tagThresholds)
      .values(values)
      .onConflictDoUpdate({ target: tagThresholds.slug, set: values });

    if (wasThere) refreshed++;
    else inserted++;
    console.log(
      `  ${wasThere ? "refresh " : "insert  "} ${pack.slug.padEnd(22)} ` +
        `low=${values.thresholdLow} high=${values.thresholdHigh} floor=${values.sigmoidFloor}`,
    );
  }

  console.log(
    `\ndone: ${inserted} inserted, ${refreshed} refreshed, ` +
      `${calibrated.size} calibrated rows preserved`,
  );
  console.log(`packs_version: ${packsVersion}`);
  console.log(
    "\nNOTE: every threshold above is a GUESS until calibrate.py runs against\n" +
      "labelled images. `calibrated: false` is reported through the API on purpose.",
  );
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
