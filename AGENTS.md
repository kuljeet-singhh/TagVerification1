# Working in this repo

DOOH Tag Verification: does an ad creative actually contain the content it is tagged with?
It is a compliance tool. Read `README.md` first — the "Things worth knowing" section at the
bottom is not background colour, it is the set of invariants that make the product correct.

This was a Next.js app until it was rewritten in Python. If you find TypeScript, Drizzle,
Vercel or `npm` referenced anywhere, it is stale and should be fixed.

## Two environments, on purpose

| | `tagverify/` (web app) | `inference/` (model) |
|---|---|---|
| venv | `.venv` at the repo root | `inference/.venv` |
| deps | `pyproject.toml` | `inference/requirements.txt` |
| deployed to | Docker / any host | a Hugging Face Space, via `git subtree push` |

**Never add torch, transformers or gradio to `pyproject.toml`**, and never import
`inference.detector` from `tagverify/`. The split exists so that serving HTTP does not require
2GB of ML wheels. The one shared file is `inference/banding.py`, which imports nothing.

`av` in `pyproject.toml` is not an exception to that rule. It is a codec binding for reading
video (~18MB, ffmpeg bundled in its wheels, no system package), not an ML dependency, and it
does not cross the tiers: frame sampling happens in the web tier and the Space still only ever
receives a single still.

`inference/` runs FLAT inside the Space (`import detector`) and as a package in this repo
(`from inference.banding import ...`). That is why `detector.py` and `calibrate.py` use a
try/except import shim. Do not "simplify" it away.

The client that CALLS the model tier is `tagverify/scoring/client.py`. It used to be
`dooh/inference/client.py`, which meant two things called `inference` at different levels
meaning opposite ends of the same wire, one nested inside the other. Keep the two words apart:
**`inference/` is the tier, `scoring/` is our client for it.** The web package is `tagverify`,
not `dooh` — `dooh` names the whole domain, and three sibling projects live under `Dooh/`. The
`dooh` CLI command, the `dooh_live_` key prefix and the `dooh_admin` cookie keep their names:
the first is human-facing, and changing either of the others would invalidate every issued key
and every open session.

## Rules that are load-bearing

1. **`present: null` means uncertain, not false.** Never flatten it. Never let a UI, a
   default, or a convenience helper turn "we don't know" into "we checked and it's clean".
2. **An unknown tag is an error.** Refuse the whole request; never skip the tag and return
   the others.
3. **The sigmoid floor is checked BEFORE the score bands.** Reordering makes a photo of a
   mountain read as alcohol. The rule lives in `inference/banding.py` and nowhere else — it
   used to be written out three times and the copies drifted. Do not add a second sigmoid
   check above the floor: it was tried, and `band_of`'s docstring records why it cannot work.
4. **`calibrated: false` must stay visible** in the API and in the UI. An uncalibrated
   verdict is a guess, and presenting a guess as a measurement is the failure mode this whole
   product exists to prevent.
5. **Never store the media.** Only its sha256 — video included, which is why it is decoded
   from an in-memory buffer and never written to disk.
6. **Every tag competes against every other tag's positives.** Softmax gives its mass to a
   tag's positives when nothing else in the pool describes the image, so the catalog doubles as
   the negative space (`detector.py` `_verdict`). Two fingerprints follow from this:
   `packs_version` covers the prompts and the scoring code (`inference/versioning.py`),
   `decision_version` covers the rule and the thresholds (`tagverify/tags/decide.py`). If a change
   moves a NUMBER, it belongs in the first; if it changes how that number is READ, the second.
7. **Never expose thresholds** through `/api/v1/tags`. `decision_version` is not an
   exception: it is a truncated one-way hash of them, published so a caller can key its
   own cache on the rule that produced a verdict instead of serving a superseded one.
8. **Unknown and revoked API keys return identical responses.**
9. **Admin defaults closed.** With `ADMIN_PASSWORD` unset, `/admin` refuses rather than
   opening, and every mutating handler re-checks the session itself.
10. **Video frames are decided individually, then collapsed.** Present beats uncertain beats
   absent. Never rank frames by raw score: the sigmoid floor is a per-frame veto, so a vetoed
   0.92 frame would beat a genuine 0.60 one. The rule lives in `tagverify/analyze/aggregate.py`
   and nowhere else.
11. **A frame that fails fails the request.** No partial video verdicts, ever.
12. **The media kind is sniffed from the bytes**, never from the field name, filename or
    Content-Type — otherwise renaming a file is a way around the policy.

## Before you commit

```bash
make check              # ruff + the full pytest suite
make inference-smoke    # only if you touched inference/ — 17 golden ML cases
```

`tests/test_banding.py` is the highest-value file in the repo. If you change how a verdict is
decided, it should fail. If it doesn't, the test is wrong. `tests/test_aggregate.py` is its
counterpart for video: if you change how frames combine, that one should fail.

## Frontend

Hand-authored CSS in `tagverify/static/css/`, Jinja2 templates, HTMX vendored in
`tagverify/static/js/`. There is no build step and no `package.json` — keep it that way. All
colours and spacing come from the token block at the top of `app.css`; do not introduce
literal hex values in templates.
