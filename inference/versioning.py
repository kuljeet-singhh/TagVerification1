"""
The fingerprint of everything that determines a SCORE.

THIS IS THE SINGLE SOURCE OF TRUTH. DO NOT COPY IT.
---------------------------------------------------
It used to be written twice: once in detector.py, and once in dooh/cli.py under a comment
saying "Must match detector.py". That is the same promise banding.py's header describes --
the kind that holds right up until it doesn't -- and here it would fail silently and
expensively: calibrate.py stamps the detector's value into calibration.json, and
`dooh apply-calibration` refuses to apply when the CLI's copy disagrees.

WHY IT COVERS THE CODE AND NOT JUST THE PROMPTS
-----------------------------------------------
It used to hash packs.json alone, on the reasoning that prompts are what the model sees. But
the scores are produced by the prompts AND by the code that pools and compares them -- and
that code changes. When cross-tag competition was added to _verdict(), every score in the
product moved while a packs.json-only fingerprint sat still, which would have left every
cached raw score in the database describing a scoring rule that no longer existed, with
nothing anywhere to notice. Caches key on this value precisely so that cannot happen.

The rule of thumb: if changing it changes a NUMBER the model reports, it belongs in here. If
it changes how that number is READ, it belongs in decision_version (dooh/tags/decide.py).

WHY THIS FILE LIVES IN inference/
----------------------------------
Same reason as banding.py: inference/ is git-subtree-pushed to a Hugging Face Space as a
standalone app and cannot import from the web application, while the web application can
import from here. It therefore has to work in two layouts -- flat inside the Space
(`import versioning`) and packaged in the repo (`from inference.versioning import ...`) --
so, like banding.py, it imports nothing beyond the standard library.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

#: Length of the hex digest we publish. Matches the original packs_version, so nothing
#: downstream that stores or logs the value has to change shape.
LENGTH = 12


def scorer_version(packs: Path | str | bytes, scorer: Path | str) -> str:
    """
    Fingerprint the prompt pack and the scoring code together.

    `scorer` is detector.py -- the pooling, the crop grid, and the softmax that turn an image
    and a prompt table into a score. Pass the module's own `__file__` from inside it, so the
    value describes the code actually loaded rather than whatever is on disk right now.

    The separator is a NUL byte, which cannot occur in either file, so no concatenation of one
    file's tail with the other's head can collide with a different pair.

    `packs` may be the FILE or its BYTES, and the two are interchangeable: the same bytes give
    the same digest either way. The bytes form exists for reload_packs, where the pack arrives
    over the wire and the copy on disk is the stale one -- hashing that copy would leave the
    fingerprint sitting still while every score moved, which is the exact failure this module
    exists to prevent. Callers that hold a path should keep passing the path; nothing here
    re-serialises, so a pushed pack must be the exporter's own bytes, byte for byte.
    """
    digest = hashlib.sha256()
    digest.update(packs if isinstance(packs, bytes) else Path(packs).read_bytes())
    digest.update(b"\x00")
    digest.update(Path(scorer).read_bytes())
    return digest.hexdigest()[:LENGTH]


def surface_fingerprint(
    positives: list[str], negatives: list[str], sigmoid_floor: float | None = None
) -> str:
    """
    Fingerprint ONE tag's scoring surface -- everything about it that can move a score.

    packs_version answers "is the whole catalog the one that produced this score". This
    answers the narrower question the admin page needs: is the running model scoring THIS tag
    off what the catalog currently says? Comparing slugs cannot tell -- an edited tag keeps
    its slug, so rewriting every phrase in it raised no warning at all while the model went on
    matching the old ones.

    The three fields are the SCORING SURFACE, the same set tests/test_packs.py pins under
    that name: they are what reaches _row()/text_embeds and thresholds(). Deliberately NOT
    label, description or the `//negatives` rationale -- those reach the catalog listing and
    a human reader, never a number, and flagging a typo fix in a description as "the model is
    stale" is the always-on warning this banner exists to avoid being.

    ORDER MATTERS. Reordering phrases rewrites packs.json, which moves packs_version, so it
    has to move this too or the two would disagree about whether anything changed.

    Both tiers call THIS function -- the model over its loaded pack, the web app over the
    database row -- which is why it lives in the one module that ships flat to the Space and
    is also copied into the web image. Do not reimplement it on either side: two fingerprints
    that drift would report "stale" forever, and nobody would trust the banner again.

    It lives here rather than in detector.py for a sharper reason than tidiness. packs_version
    hashes detector.py's BYTES, so a function defined there is part of the scoring
    fingerprint -- and later tuning what this covers would move packs_version, invalidating
    every cached score and every calibration stamp, for a change that moved no score at all.
    Nothing hashes versioning.py.
    """
    digest = hashlib.sha256()
    for group in (positives, negatives):
        for phrase in group:
            digest.update(phrase.encode())
            # NUL between phrases, \x01 between the groups: without the second separator
            # ["ab"],[] and ["a","b"],[] and [],["ab"] would all hash the same.
            digest.update(b"\x00")
        digest.update(b"\x01")
    # repr() rather than str(): None and the float 0.0 must not collide, and repr round-trips
    # a float exactly where formatting could round two different floors together.
    digest.update(repr(sigmoid_floor).encode())
    return digest.hexdigest()[:LENGTH]
