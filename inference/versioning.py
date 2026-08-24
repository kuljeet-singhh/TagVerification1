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


def scorer_version(packs: Path | str, scorer: Path | str) -> str:
    """
    Fingerprint the prompt pack and the scoring code together.

    `scorer` is detector.py -- the pooling, the crop grid, and the softmax that turn an image
    and a prompt table into a score. Pass the module's own `__file__` from inside it, so the
    value describes the code actually loaded rather than whatever is on disk right now.

    The separator is a NUL byte, which cannot occur in either file, so no concatenation of one
    file's tail with the other's head can collide with a different pair.
    """
    digest = hashlib.sha256()
    digest.update(Path(packs).read_bytes())
    digest.update(b"\x00")
    digest.update(Path(scorer).read_bytes())
    return digest.hexdigest()[:LENGTH]
