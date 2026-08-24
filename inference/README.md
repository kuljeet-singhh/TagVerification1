---
title: DOOH Tag Verification
emoji: 🍺
colorFrom: indigo
colorTo: purple
sdk: gradio
app_file: app.py
pinned: false
short_description: Verify a creative contains the content it is tagged with
---

# DOOH Tag Verification — inference service

Answers one question: **does this image actually contain the content it claims?**

Given an image and a list of tags (`alcohol`, `gym_fitness`, `protein_supplements`, …)
it returns a calibrated present / absent / uncertain verdict per tag, with a
confidence score and the evidence behind the call.

There is **no Dockerfile**. `sdk: gradio` in the front-matter above tells Hugging
Face to install `requirements.txt` and run `app.py`. You never need Docker
locally or in CI.

## Run locally

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
./.venv/bin/python app.py          # http://127.0.0.1:7860
```

First run downloads ~400MB of SigLIP 2 weights into the HF cache. After that
startup is a few seconds.

## Test

```bash
./.venv/bin/python smoke_test.py
```

Downloads real photos from Wikipedia and asserts verdicts. The **negative** cases
carry the weight: a detector that answers "yes" to everything scores 100% recall
and is useless. Orange juice must not read as alcohol, and infant formula must
not read as protein powder.

Exit codes: `0` pass, `1` a verdict was wrong, `2` images couldn't be fetched
(results incomplete — not a pass).

## API

Three endpoints, all via Gradio's queue protocol
(`POST /gradio_api/call/<name>` → `{event_id}`, then
`GET /gradio_api/call/<name>/<event_id>` for an SSE result). From TypeScript,
use `@gradio/client` rather than hand-rolling that.

| endpoint | in | out |
|---|---|---|
| `analyze` | base64 image string, `string[]` tags | per-tag verdicts |
| `health` | — | model + packs version, tag list |
| `tags` | — | tag catalog with descriptions |

`analyze` takes **base64**, not an uploaded file, deliberately: it keeps the call
to one round trip instead of Gradio's upload-then-submit dance.

```jsonc
// POST /gradio_api/call/analyze  ->  {"data": ["<base64>", ["alcohol"]]}
{
  "model": "google/siglip2-base-patch16-224",
  "packs_version": "1a2db18ba940",
  "latency_ms": 612.4,
  "results": [{
    "tag": "alcohol",
    "present": true,            // true | false | null (null = escalate)
    "score": 0.9772,            // softmax mass on positives — the decision signal
    "sigmoid": 0.028,           // absolute resemblance — a low backstop only
    "band": "present",
    "confidence": "high",
    "decided_by": "siglip",
    "top_phrase": "a glass of beer with foam",
    "crop": [0.0, 0.5, 0.5, 1.0]   // where it was found, as 0-1 fractions
  }]
}
```

`present: null` means SigLIP is genuinely unsure and the caller should escalate
to a vision LLM. It happens exactly one way: the score landed between
`threshold_low` and `threshold_high`. A sigmoid below `sigmoid_floor` is a
veto, not an escalation — it returns `present: false` with
`decided_by: "sigmoid_floor"`. (A second "near-floor" band above the floor was
tried and removed; `banding.py`'s docstring records why it cannot work, and
`tests/test_banding.py` asserts it does not come back.) Errors arrive as
`event: error` with a message; unknown tags are rejected and echo the valid
list, because "unknown tag" and "content absent" must never look alike.

## Adding or changing a tag

Edit **`packs.json`** only. Never touch Python.

```jsonc
{
  "slug": "my_tag",
  "label": "My tag",
  "positives": ["a photo-like phrase describing the thing"],
  "negatives": ["the confusable neighbour that is NOT the thing"]
}
```

Two rules, both learned the hard way from real measurements:

1. **Mirror the phrasings.** If a positive says *"a scoop of protein powder"*, a
   negative must say *"a scoop of <something else>"*. With only *"a tin of baby
   formula"* as the negative, a photo of a scoop of formula scored **0.709**
   (wrongly present) — because it matched the positive on the word "scoop"
   alone. Adding *"a scoop of baby formula milk powder"* dropped it to **0.224**
   while genuine whey protein stayed at **0.999**.

2. **Name the confusable neighbour**, not something random. `a plain brick wall`
   teaches the model nothing about alcohol; `a bottle of fruit juice` is what
   actually gets mistaken for it.

Any edit changes `packs_version` (a hash of the file), so a stored verdict can
always be traced to the exact config that produced it.

## How scoring works

Full detail is commented in `detector.py`. In short:

1. **Multi-crop** — score the whole image plus 9 overlapping half-size windows,
   keep the best. SigLIP sees 224×224, so a beer bottle in the corner of a 4K
   billboard becomes eight pixels of mush without this. Biggest single accuracy
   win in the pipeline, and the winning window becomes the evidence crop.
2. **Softmax** over `positives + hard negatives + shared distractors`. The score
   is the mass landing on positives. This is what makes hard negatives work —
   juice only loses to whiskey if juice is in the running.
3. **Sigmoid floor** as a weak veto. Softmax must sum to 1, so an image of a
   mountain can hand 0.5 to "a glass of beer" purely by beating equally
   irrelevant options. Measured true positives run 0.028–0.902 and true
   negatives 0.000, so the floor sits very low (0.005) — it is a backstop, not
   a gate. Set it higher and you veto real detections.

## Calibration

The numbers in `packs.json` are **educated guesses**. Real values come from
`calibrate.py`.

```bash
# 1. put labelled images here (real creatives beat stock photos)
#      eval/<tag>/pos/   images that DO contain the tag
#      eval/<tag>/neg/   images that do NOT — use NEAR MISSES
#    or fetch a demo set to see the workflow:
./.venv/bin/python fetch_demo_eval.py

# 2. measure
./.venv/bin/python calibrate.py                    # all tags
./.venv/bin/python calibrate.py alcohol            # one tag
./.venv/bin/python calibrate.py --max-escalation 0.5

# 3. review calibration.json, then load it into Postgres (from the repo root)
dooh apply-calibration --dry-run
dooh apply-calibration
```

**How many images.** At least 8 per side per tag or the tag is scored but not
marked calibrated — 3 correct calls out of 3 is not 100% precision. The report
prints a 95% confidence lower bound alongside the measured figure; that bound is
the number to quote. Perfect precision on 10 positives bounds at 72%, on 20 at
84%, and needs ~35 to support a 90% claim. More labelled images is the only fix.

**Read the failures, they are specific.** When a tag cannot reach its target the
report names the negatives blocking it. A real example from this repo: a photo of
a **hamburger scored 0.939 for `alcohol`**, above champagne and wine, because the
positives describe bar *scenes* and pub food matches the setting. Adding four food
negatives dropped it out of contention and moved recall from 50% to 60%. That is
the loop — the tool tells you which prompt is missing.

**Beware overfitting.** With 20 images you can tune until everything passes and
learn nothing. Negatives should name a genuinely confusable *category* ("an energy
drink can"), not be reverse-engineered from one file that happened to fail.

**Re-run after editing a pack.** Thresholds are only valid for the prompts they
were measured against. Changing `packs.json` changes `packs_version`; the API
notices the mismatch and reports affected tags as uncalibrated rather than
applying stale cutoffs to different numbers.

Measured on an M-series Mac: ~600 ms for 10 crops. Expect roughly 2–3× that on
the Space's 2 free vCPUs. If it proves too slow, `Detector(grid=2)` drops to
5 crops.
