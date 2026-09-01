"""
Config -> the scorer that is actually answering. One place, because two would drift.

`analyze/run.py` needs this to score, and `api/v1/health.py` needs it to report. When those
two answered the question separately, `/health` cheerfully reported a warm SigLIP Space on a
box where a VLM was doing every verdict — a green tick for a component that was not in the
path. For a compliance tool, monitoring that describes the wrong dependency is worse than no
monitoring, because it is believed.

Imports are inside the functions on purpose: a checkout that has never configured a VLM must
not pay for importing its SDK, and `tagverify/scoring/vlm.py` imports from `client.py`, so
resolving at module scope would make the import graph care about the order of these files.
"""

from __future__ import annotations

from typing import Any

from tagverify.config import settings

#: Every value `SCORER` may take. An unknown one is refused rather than defaulted, so a typo
#: in an environment file is a loud failure at the first request instead of a silent fall back
#: to a scorer nobody asked for.
KNOWN = ("vlm", "fake")


def name() -> str:
    """The active scorer, normalised."""
    return (settings().scorer or "vlm").strip().lower()


def provider() -> str:
    """Which VLM implementation answers. Meaningless unless `name()` is "vlm"."""
    return (settings().vlm_provider or "anthropic").strip().lower()


def module() -> Any:
    """
    The module doing the scoring: it exposes `health(session)` and `score_frames(intake, h)`.

    Returns None for SigLIP, whose two halves live in `scoring/client.py` and in `run.py`'s
    `_score_frames` and are deliberately left alone — the point of this whole change is to be
    able to compare against an untouched baseline.
    """
    match name():
        case "vlm":
            if provider() == "gemini":
                from tagverify.scoring import gemini

                return gemini

            from tagverify.scoring import vlm

            return vlm
        case "fake":
            from tagverify.scoring import fake

            return fake
        case _:
            return None


def rule() -> str:
    """
    The scorer's contribution to `decision_version`: prompt, schema and confidence bands.

    EMPTY FOR SIGLIP, and that is load-bearing rather than tidy — `decision_version` appends
    this only when non-empty, so shipping a VLM path that nobody has switched on leaves every
    cached verdict here and in dooh-backend exactly where it was. See decide.decision_version.

    The PROVIDER is deliberately absent. Two providers reading the same prompt through the
    same schema apply the same rule; which one answered is a property of the model, and that
    already travels in `packs_version` via `catalog_fingerprint`. Folding it in here as well
    would re-key every verdict on a provider switch twice over, for one real change.
    """
    if name() not in {"vlm", "fake"}:
        return ""

    from tagverify.scoring import prompt
    from tagverify.tags import decide

    return (
        f"{prompt.PROMPT_VERSION}:{prompt.SCHEMA_VERSION}"
        f":{decide.VLM_HIGH}:{decide.VLM_MEDIUM}"
    )


def describe() -> str:
    """A one-line label for logs and `/api/v1/health`."""
    if name() == "vlm":
        return f"vlm:{provider()}:{settings().vlm_model}"
    return name()


def model_label() -> str:
    """
    The scorer's name as a person would say it, for the admin UI.

    `describe()` is the precise, greppable form and belongs in `/api/v1/health`, where a
    monitoring tool needs the mechanism and the provider as well as the model. Reading
    "vlm:gemini:gemini-3.7-flash is scoring" in a sentence is worse than reading
    "gemini-3.7-flash is scoring", and the extra precision answers nothing a person on that
    page is asking.
    """
    if name() == "vlm":
        return settings().vlm_model
    return name()
