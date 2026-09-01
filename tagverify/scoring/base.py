"""
What every scorer has to answer, regardless of what is doing the scoring.

WHY THIS EXISTS
---------------
`analyze/run.py` consumed three fields off the Space's `/health` -- the tag list (which gates
AGENTS.md rule 2), the pack fingerprint (which is half the cache key) and the model name --
and a list of raw per-tag rows from `analyze_image`. Those two shapes, and nothing else, are
what the pipeline actually depends on. Naming them here is what lets a second scorer exist
without `run.py`, `aggregate.py`, the cache, the audit row or the response assembly changing
at all.

It is deliberately a dataclass and a convention rather than an ABC. Each scorer is a module
with `health()` and `score_frames()`, which is how `scoring/client.py` was already shaped;
inheritance would buy nothing and would make the SigLIP path harder to leave alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class ScorerHealth:
    """
    The three things `analyze/run.py` needs before it can score anything.

    Under SigLIP all three come from the Space, which is why deleting the Space takes the
    unknown-tag gate and the cache key with it unless something else supplies them. Under a
    VLM they come from Postgres and config, computed in this tier.
    """

    #: Every tag the scorer can answer for. `run.py` refuses the WHOLE request if a caller
    #: names one that is not here -- AGENTS.md rule 2. "We didn't check" and "we checked and
    #: it's clean" mean opposite things, so an unknown tag is never silently skipped.
    tags: list[str]

    #: Fingerprint of what the scorer was asked. Half of the cache key on
    #: `creative_tag_analyses`, here and in dooh-backend, and the staleness input to
    #: `decide()`. It keeps its name across the swap because its MEANING is unchanged: when
    #: this moves, previously-cached verdicts no longer describe the current question.
    packs_version: str

    #: What produced the verdict, reported to the caller. A model id, not a mechanism --
    #: `decided_by` carries the mechanism.
    model: str

    #: slug -> the sentence the model is asked to judge. Empty for SigLIP, whose question is
    #: a phrase pack encoded inside the Space rather than text we send. Carried here so the
    #: scoring call does not need a second database read on the hot path.
    specs: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------- errors
#
# These moved here from scoring/client.py when the SigLIP tier was removed. They were
# never Gradio concepts: they mean "retry, this is expected" and "the scorer failed",
# which every scorer needs and every caller in the codebase already translates into the
# right HTTP status, playground message and admin banner. Keeping the names is what let
# that whole translation layer survive the swap untouched.


class InferenceWarming(Exception):
    """
    The scorer is temporarily unable to answer, and will be able to shortly.

    Callers surface a retryable 503 with Retry-After, not a generic failure. It named a
    sleeping Space reloading 400MB of weights; it now names a provider returning 429 or 503
    under load. Same shape of problem, same correct response: wait and ask again.
    """

    def __init__(self, message: str, retry_after: int = 30) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class InferenceError(Exception):
    """
    The scorer failed, or answered something we will not act on.

    A refusal, a timeout, a malformed reply, a short frame list. Never a partial or optimistic
    result: raising is what makes the creative FLAG downstream rather than pass, which is the
    single failure mode this product exists to prevent.
    """
