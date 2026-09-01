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
outlived the code. The model itself is preserved on `main`.

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
4. **`calibrated: false` must stay visible** in the API and in the UI. An uncalibrated
   verdict is a guess, and presenting a guess as a measurement is the failure mode this whole
   product exists to prevent.
5. **Never store the media.** Only its sha256 — video included, which is why it is decoded
   from an in-memory buffer and never written to disk.
6. **A tag is its description, and the description is the prompt.** It is sent to the model
   verbatim and is the whole of what is asked, so description quality IS tag quality -- there
   is no threshold left to tune afterwards. Two fingerprints still split the same way:
   `packs_version` covers the question (model id + prompt + every tag's description,
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
