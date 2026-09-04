# Working in this repo

DOOH Tag Verification: does an ad creative actually contain the content it is tagged with?
It is a compliance tool. Read `README.md` first — the "Things worth knowing" section at the
bottom is not background colour, it is the set of invariants that make the product correct.

This was a Next.js app until it was rewritten in Python. If you find TypeScript, Drizzle,
Vercel or `npm` referenced anywhere, it is stale and should be fixed.

## One environment, now

There used to be two, on purpose: `.venv` served HTTP and `inference/.venv` ran a SigLIP 2
model deployed to a Hugging Face Space, and the rule was never to let the ML stack cross into
the web tier. That split, the Space, the prompt pack and the publish step that carried one to
the other are all gone -- a model that reads its prompt per request needs none of them. One
venv, one `pyproject.toml`, one deploy.

`inference/` survives as **data only**: the labelled eval images, and the measurements taken
over them. `inference/gate_cache.json` holds the SigLIP baseline the removal was judged
against -- 146 cross-tag false blocks, 0.688 mean recall over 466 images -- so the comparison
outlived the code. `inference/phrase_packs_archive.json` holds the packs themselves, dumped
from `content_tags` the moment before migration 0003 dropped the columns: 158 positives and
179 mirrored negatives across 24 tags, with the four rationales recording what was measured.
Same principle in both files -- the measurement outlives the code. The model itself is
preserved on `siglip-archive`.

## Rules that are load-bearing

1. **`present: null` means uncertain, not false.** Never flatten it. Never let a UI, a
   default, or a convenience helper turn "we don't know" into "we checked and it's clean".
2. **An unknown tag is an error.** Refuse the whole request; never skip the tag and return
   the others.
3. **There is no banding left, and that is the point.** A similarity score meant nothing on
   its own -- measured true positives ran 0.028 to 0.902 -- so a verdict used to be two cutoffs
   and an absolute-resemblance floor, and every tag needed calibrating first. The model answers
   the question now, so `decide()` carries `present` out unchanged. Do not reintroduce a
   threshold on `score`: it is the model's own self-reported confidence, a different quantity
   from a similarity, and nothing enforces on it.

   Migration 0004 dropped the cutoffs themselves (`tag_thresholds.threshold_low`,
   `threshold_high`, `sigmoid_floor`, and the long-dead `escalate`). The admin form over them
   was not merely inert: every field of `Thresholds` folds into `decision_version`, so saving
   a number nothing read re-keyed every cached verdict here and in dooh-backend, and the same
   write set `calibrated = false` with nothing left able to set it back.
4. **`calibrated: false` must stay visible** in the API and in the UI. An uncalibrated
   verdict is a guess, and presenting a guess as a measurement is the failure mode this whole
   product exists to prevent. The UI half of that is the VERDICT surface -- the playground's
   card and `partials/results.html` -- not a catalog-wide report; /admin's read-only
   calibration tab was removed, and the rule is untouched by that.

   **A SUPERSEDED measurement is the same failure, and it is the one that actually happened.**
   A calibration taken against a different `packs_version` is stale, not calibrated. Compute it
   with `decide.effective_calibrated` and NEVER read `TagThreshold.calibrated` raw -- that
   comparison was written out three times, the admin panel's copy did not do it, and the page
   rendered eight tags as measured, precision and recall beside them, while every API response
   for those same tags said `calibrated: false`. One function; every surface calls it. That
   panel is gone now, but the rule outlived it and so did its test --
   `tests/test_calibration.py` compares `/api/v1/tags` against the helper per tag.

   Nothing writes `tag_thresholds` today: `apply-calibration` went with the ranking model and
   the hand-edit form went with the thresholds, so every tag reads uncalibrated and that is
   honest. The admin tab that reported it went too -- a whole panel showing 0 of 25 measured
   is a constant, not a report -- and it comes back with the writer, not before it.
   `docs/VLM_SCORING.md` §5.2 has the eval harness writing those columns back; the table is
   kept unchanged in shape so that lands as a writer, not a migration.
5. **Never store the media.** Only its sha256 — video included, which is why it is decoded
   from an in-memory buffer and never written to disk.
6. **A tag is its NAME, and the name is the prompt.** `label` is what the model is told to
   look for -- rendered as `- alcohol: Alcoholic content` -- so name quality IS tag quality,
   and there is no threshold left to tune afterwards. `scoring/prompt.py:catalog_specs` is the
   single place that decides which column that is; there were three copies of it and changing
   the question meant changing all three and hoping.

   **`description` is NOT part of the question.** It was until the name-only change, and every
   doc in this repo said so, which is why the wording is worth being blunt about: it is now
   what a screen owner reads in DOOH's blocked-categories picker, it is returned by
   `/api/v1/tags`, and editing it moves no fingerprint and re-scores nothing.

   That trade was made deliberately and its cost is real: a tag's boundary can no longer be
   written down. `sugary_drinks` used to say packaged juice counts and now does not; the model
   decides where `revealing_clothing` starts. **The remaining lever is the name**, so a
   misfiring tag is fixed by renaming it -- which correctly re-keys its cached verdicts -- and
   `dooh gate` over `inference/eval/` is the only way to find out whether it worked.

   Nothing else on the row is asked either: migration 0003 dropped `positives`, `negatives`,
   `rationale` and `content_tags.sigmoid_floor`, which were SigLIP's question and had been read
   by nothing since f357a1c. Neither fingerprint ever covered them, so the drop re-keyed no
   verdict. `/api/v1/tags` still ACCEPTS all four and ignores them, because dooh-backend still
   sends them; do not turn that into a 422 without changing dooh-backend first.

   Two fingerprints still split the same way:
   `packs_version` covers the question (model id + prompt + every tag's slug and NAME,
   `scoring/prompt.py`), `decision_version` covers how the answer is read
   (`tags/decide.py`). If a change moves what is ASKED it belongs in the first; if it changes
   how the answer is INTERPRETED, the second.
7. **Never expose thresholds** through `/api/v1/tags`. `decision_version` is not an
   exception: it is a truncated one-way hash of them, published so a caller can key its
   own cache on the rule that produced a verdict instead of serving a superseded one.
8. **Unknown and revoked API keys return identical responses.**
9. **Admin defaults closed.** With `ADMIN_PASSWORD` unset, `/admin` refuses rather than
   opening, and every mutating handler re-checks the session itself.
10. **Video frames are decided individually, then collapsed.** Present beats uncertain beats
   absent, and band comes before score. All six frames now travel in ONE request, which is a
   transport detail and must not become a decision-making one: the schema carries a verdict
   per frame per tag, and the prompt asks for per-frame independence explicitly. The collapse
   lives in `tagverify/analyze/aggregate.py` and nowhere else.
11. **A frame that fails fails the request.** No partial video verdicts, ever.
12. **The media kind is sniffed from the bytes**, never from the field name, filename or
    Content-Type — otherwise renaming a file is a way around the policy.

## Before you commit

```bash
make check              # ruff + the full pytest suite
```

`make inference-smoke` is gone with the model tier. `tests/test_aggregate.py` is now the
highest-value file in the repo: if you change how video frames combine, it should fail. Its
former counterpart `tests/test_banding.py` was deleted with the rule it guarded -- there is no
score to band any more.

The suite pins `SCORER=fake` in `tests/conftest.py`, unconditionally. A real environment
variable outranks `.env.local`, and that is the only way to stop a developer's own
configuration deciding what the suite tests -- which happened, turning ten tests red on one
machine and green on another. Tests needing a real model are marked `needs_inference` and skip
under the fake.

## Frontend

Hand-authored CSS in `tagverify/static/css/`, Jinja2 templates, HTMX vendored in
`tagverify/static/js/`. There is no build step and no `package.json` — keep it that way. All
colours and spacing come from the token block at the top of `app.css`; do not introduce
literal hex values in templates.
