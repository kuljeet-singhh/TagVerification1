/**
 * Unit tests for the decision layer.
 *
 *   npx tsx scripts/smoke-decide.mts
 *
 * No database or inference service needed — decide() is pure. These cover the
 * three rules that are easy to break and expensive to get wrong: the sigmoid
 * veto ordering, the uncertain band, and the stale-pack guard.
 */
import { decide } from "@/lib/tags/decide";
import type { TagThreshold } from "@/lib/db/schema";
import type { RawVerdict } from "@/lib/inference/client";

let passed = 0;
let failed = 0;

function check(name: string, condition: boolean, detail = "") {
  if (condition) {
    passed++;
    console.log(`  PASS  ${name}`);
  } else {
    failed++;
    console.log(`  FAIL  ${name} ${detail}`);
  }
}

const threshold = (over: Partial<TagThreshold> = {}): TagThreshold =>
  ({
    slug: "alcohol",
    thresholdLow: 0.25,
    thresholdHigh: 0.78,
    sigmoidFloor: 0.005,
    escalate: true,
    calibrated: true,
    precision: 0.61,
    recall: 0.6,
    packsVersionSeen: "a5788f8d8e1d",
    updatedAt: new Date(),
    updatedBy: "calibrate",
    ...over,
  }) as TagThreshold;

const raw = (over: Partial<RawVerdict> = {}): RawVerdict =>
  ({
    tag: "alcohol",
    present: true,
    score: 0.9,
    sigmoid: 0.5,
    band: "present",
    confidence: "high",
    decided_by: "siglip",
    top_phrase: "a glass of beer with foam",
    crop: [0, 0, 1, 1],
    ...over,
  }) as RawVerdict;

console.log("\nbanding");
check("score above high -> present", decide(raw({ score: 0.9 }), threshold()).present === true);
check("score below low -> absent", decide(raw({ score: 0.1 }), threshold()).present === false);
check(
  "score inside the band -> uncertain (null, NOT false)",
  decide(raw({ score: 0.5 }), threshold()).present === null,
);
check(
  "score exactly at high -> present (inclusive)",
  decide(raw({ score: 0.78 }), threshold()).present === true,
);
check(
  "score exactly at low -> absent (inclusive)",
  decide(raw({ score: 0.25 }), threshold()).present === false,
);

console.log("\nsigmoid floor is a veto, checked FIRST");
{
  // High score but nothing in the image actually resembles the tag: softmax mass
  // can concentrate purely by beating equally-irrelevant options.
  const v = decide(raw({ score: 0.99, sigmoid: 0.0 }), threshold());
  check("high score + zero sigmoid -> absent", v.present === false, `got ${v.present}`);
  check("attributed to sigmoid_floor", v.decidedBy === "sigmoid_floor");
}
check(
  "a genuine low-sigmoid positive is NOT vetoed (beer measured at 0.028)",
  decide(raw({ score: 0.9, sigmoid: 0.028 }), threshold()).present === true,
);

console.log("\nstale-pack guard");
check(
  "matching pack version stays calibrated",
  decide(raw(), threshold(), "a5788f8d8e1d").calibrated === true,
);
check(
  "pack changed after calibration -> reported uncalibrated",
  decide(raw(), threshold(), "different_hash").calibrated === false,
);
check(
  "stale thresholds are still APPLIED (best available), just not trusted",
  decide(raw({ score: 0.9 }), threshold(), "different_hash").present === true,
);
check(
  "no live version supplied -> no false staleness alarm",
  decide(raw(), threshold(), undefined).calibrated === true,
);
check(
  "never-calibrated tag stays uncalibrated even when versions match",
  decide(raw(), threshold({ calibrated: false }), "a5788f8d8e1d").calibrated === false,
);

console.log("\nmissing threshold row");
{
  const v = decide(raw({ score: 0.9 }), undefined, "a5788f8d8e1d");
  check("falls back to defaults", v.present === true);
  check("and is reported uncalibrated", v.calibrated === false);
}

console.log(`\n${passed} passed, ${failed} failed\n`);
process.exit(failed > 0 ? 1 : 0);
