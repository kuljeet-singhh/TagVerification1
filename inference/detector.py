"""
SigLIP 2 tag detector.

This is the only file in the project that needs real ML understanding. It is
commented heavily on purpose. Everything else (adding tags, tuning thresholds,
changing the API) happens in JSON or TypeScript.

--------------------------------------------------------------------------
THE CORE IDEA
--------------------------------------------------------------------------
SigLIP 2 is a "two-tower" model. It has two separate encoders:

    image  -> [image encoder] -> vector of 768 numbers
    text   -> [text encoder ] -> vector of 768 numbers

Both land in the SAME 768-dimensional space. If a picture and a sentence mean
the same thing, their two vectors point in roughly the same direction. So
"does this image contain alcohol?" becomes "is this image's vector pointing the
same way as the vector for 'a glass of beer with foam'?" -- which is a dot
product, i.e. one multiplication.

We never train anything. Adding a tag = writing sentences.

--------------------------------------------------------------------------
WHY WE USE *BOTH* SOFTMAX AND SIGMOID
--------------------------------------------------------------------------
Two different questions have to be answered, and one number cannot answer both:

  softmax over [positives + negatives + distractors]  ->  RELATIVE question:
      "given these options, which best describes the image?"
      This is what makes hard negatives work. A juice bottle only loses to
      whiskey if juice is in the running.

  sigmoid on the positive logits alone               ->  ABSOLUTE question:
      "does this image resemble beer AT ALL?"

You need the absolute check as a guard. Consider a photo of a mountain scored
against the alcohol pack: every option is wrong, but softmax must still sum to
1.0, so the mass gets shared out and "a glass of beer" might collect 0.5 purely
by beating equally-irrelevant options. Softmax alone would call a mountain
alcoholic. The sigmoid floor vetoes that: if nothing crosses a minimum absolute
resemblance, the answer is absent regardless of how the softmax landed.

Shared distractors ("a plain empty background", ...) help with the same problem
by giving irrelevant images somewhere for their probability mass to go.
"""

import hashlib
import json
from dataclasses import dataclass, asdict
from pathlib import Path

import torch
from PIL import Image
from transformers import AutoModel, AutoProcessor

MODEL_ID = "google/siglip2-base-patch16-224"

# SigLIP was TRAINED with every caption padded to exactly 64 tokens. If you pad
# differently the text embeddings shift and scores quietly degrade -- no error,
# just worse results. This is the single easiest way to get bad output.
MAX_TEXT_LEN = 64


def _pooled(output) -> torch.Tensor:
    """
    Pull the embedding tensor out of a model call.

    transformers <5 returned a plain Tensor from get_text_features /
    get_image_features; v5 returns a BaseModelOutputWithPooling whose
    `pooler_output` holds the embedding. HF Spaces may pin either version, so
    accept both rather than depending on one.
    """
    if isinstance(output, torch.Tensor):
        return output
    return output.pooler_output


@dataclass
class TagVerdict:
    """One tag's answer for one image."""

    tag: str
    present: bool | None  # None == uncertain, needs escalation
    score: float  # softmax positive mass, 0-1, the calibrated signal
    sigmoid: float  # strongest absolute positive match, 0-1
    band: str  # "present" | "absent" | "uncertain"
    confidence: str  # "high" | "medium" | "low"
    decided_by: str  # "siglip" | "sigmoid_floor"
    top_phrase: str  # which positive matched best -- the human explanation
    crop: list[float]  # [x0, y0, x1, y1] as 0-1 fractions of the image

    def to_dict(self) -> dict:
        return asdict(self)


def build_crops(image: Image.Image, grid: int = 3) -> list[tuple[Image.Image, list[float]]]:
    """
    Return the full image plus a set of OVERLAPPING window crops.

    Why crop at all? SigLIP resizes whatever you give it down to 224x224. Feed
    it a 4K billboard and a beer bottle in the corner becomes about eight pixels
    of brown mush -- undetectable. Scoring zoomed-in windows separately and
    keeping the best score recovers small objects. In testing this is the single
    biggest accuracy win in the pipeline.

    The windows are half the image on each side, stepped by a quarter. That
    overlap matters: with non-overlapping tiles an object sitting on a tile
    boundary gets sliced in half and may be missed in every tile. Overlapping
    windows guarantee anything smaller than a quarter-image sits fully inside at
    least one window.

    grid=3 gives 9 windows + 1 full image = 10 crops, all scored in a single
    batched forward pass.
    """
    width, height = image.size
    size = 0.5  # each window covers half the width and half the height
    crops: list[tuple[Image.Image, list[float]]] = [
        (image, [0.0, 0.0, 1.0, 1.0])  # index 0 is always the whole image
    ]

    # With grid=3 the step is (1 - 0.5) / 2 = 0.25, so offsets are 0, 0.25, 0.5.
    step = (1.0 - size) / (grid - 1) if grid > 1 else 0.0

    for row in range(grid):
        for col in range(grid):
            x0, y0 = col * step, row * step
            x1, y1 = x0 + size, y0 + size
            box = (
                int(x0 * width),
                int(y0 * height),
                int(x1 * width),
                int(y1 * height),
            )
            crops.append((image.crop(box), [x0, y0, x1, y1]))

    return crops


class Detector:
    """
    Loads SigLIP 2 and the tag packs, and pre-computes every text embedding up
    front so that a request only has to run the IMAGE encoder.

    That pre-computation is the whole performance trick. The text side never
    changes between requests, so encoding ~250 prompts on every call would be
    pure waste. Done once at startup, a request becomes: one image forward pass
    plus a matrix multiply. That is the difference between ~300ms and several
    seconds on the 2 free CPU cores we get on Hugging Face Spaces.
    """

    def __init__(self, packs_path: str | Path = "packs.json", grid: int = 3):
        self.grid = grid

        raw_packs = Path(packs_path).read_text()
        config = json.loads(raw_packs)

        self.model_id = MODEL_ID
        # Fingerprint of the pack file. Any edit to a prompt or threshold changes
        # this, so a stored verdict can always be traced back to the exact
        # configuration that produced it. Thresholds change verdicts, so this
        # needs to be auditable.
        self.packs_version = hashlib.sha256(raw_packs.encode()).hexdigest()[:12]

        self.template: str = config["prompt_template"]
        self.defaults: dict = config["defaults"]
        self.packs: dict[str, dict] = {tag["slug"]: tag for tag in config["tags"]}

        print(f"[detector] loading {MODEL_ID} ...")
        self.model = AutoModel.from_pretrained(MODEL_ID)
        self.model.eval()  # inference only; disables dropout etc.
        self.processor = AutoProcessor.from_pretrained(MODEL_ID)

        # SigLIP turns a similarity into a logit with a learned scale and bias:
        #     logit = exp(logit_scale) * cosine_similarity + logit_bias
        # We pull them out once. Note the bias is a single constant added to
        # every logit, so it cancels inside softmax -- but it genuinely matters
        # for the sigmoid, which is why we keep it.
        self.logit_scale = self.model.logit_scale.exp().item()
        self.logit_bias = self.model.logit_bias.item()

        # ---- build one flat table of every prompt we will ever need ----------
        # Rows are shared: if two packs use the same phrase it is encoded once.
        self._prompt_rows: list[str] = []
        self._row_of: dict[str, int] = {}
        self.index: dict[str, dict[str, list[int]]] = {}

        self._distractor_rows = [self._row(p) for p in config["shared_distractors"]]

        for slug, pack in self.packs.items():
            self.index[slug] = {
                "pos": [self._row(p) for p in pack["positives"]],
                "neg": [self._row(p) for p in pack["negatives"]],
            }

        self.text_embeds = self._encode_texts(self._prompt_rows)
        print(
            f"[detector] ready: {len(self.packs)} tags, "
            f"{len(self._prompt_rows)} unique prompts, dim={self.text_embeds.shape[1]}"
        )

    # ------------------------------------------------------------------ setup

    def _row(self, phrase: str) -> int:
        """Intern a phrase into the prompt table, returning its row index."""
        if phrase not in self._row_of:
            self._row_of[phrase] = len(self._prompt_rows)
            self._prompt_rows.append(phrase)
        return self._row_of[phrase]

    @torch.inference_mode()
    def _encode_texts(self, phrases: list[str]) -> torch.Tensor:
        """Encode phrases into L2-normalised embeddings, shape (n_phrases, dim)."""
        # The template ("This is a photo of {}.") matches what the HF zero-shot
        # pipeline uses, which is in turn close to SigLIP's training captions.
        # Matching the training distribution measurably helps.
        sentences = [self.template.format(p) for p in phrases]

        inputs = self.processor(
            text=sentences,
            padding="max_length",  # see MAX_TEXT_LEN note at top of file
            max_length=MAX_TEXT_LEN,
            truncation=True,
            return_tensors="pt",
        )
        embeds = _pooled(self.model.get_text_features(**inputs))

        # Normalise to unit length so a dot product IS the cosine similarity.
        return embeds / embeds.norm(p=2, dim=-1, keepdim=True)

    @torch.inference_mode()
    def _encode_images(self, images: list[Image.Image]) -> torch.Tensor:
        """Encode images into L2-normalised embeddings, shape (n_images, dim)."""
        inputs = self.processor(images=images, return_tensors="pt")
        embeds = _pooled(self.model.get_image_features(**inputs))
        return embeds / embeds.norm(p=2, dim=-1, keepdim=True)

    # ---------------------------------------------------------------- scoring

    def thresholds(self, slug: str) -> tuple[float, float, float]:
        """Per-tag thresholds, falling back to the defaults block in packs.json."""
        pack = self.packs[slug]
        return (
            pack.get("threshold_low", self.defaults["threshold_low"]),
            pack.get("threshold_high", self.defaults["threshold_high"]),
            pack.get("sigmoid_floor", self.defaults["sigmoid_floor"]),
        )

    @torch.inference_mode()
    def analyze(self, image: Image.Image, slugs: list[str]) -> list[TagVerdict]:
        """
        Score one image against the named tags.

        Unknown slugs are the caller's bug and are raised, never silently
        skipped -- a typo'd tag must not come back looking like "content absent".
        """
        unknown = [s for s in slugs if s not in self.packs]
        if unknown:
            raise KeyError(
                f"unknown tag(s): {unknown}. known: {sorted(self.packs)}"
            )

        image = image.convert("RGB")
        crops = build_crops(image, self.grid)

        # One batched forward pass for all 10 crops.
        image_embeds = self._encode_images([c for c, _ in crops])

        # Cosine similarity of every crop against every prompt, turned into
        # SigLIP logits. Shape: (n_crops, n_prompts).
        logits = self.logit_scale * (image_embeds @ self.text_embeds.T) + self.logit_bias

        return [self._verdict(slug, logits, crops) for slug in slugs]

    def _verdict(
        self,
        slug: str,
        logits: torch.Tensor,
        crops: list[tuple[Image.Image, list[float]]],
    ) -> TagVerdict:
        pos = self.index[slug]["pos"]
        neg = self.index[slug]["neg"]
        columns = pos + neg + self._distractor_rows

        # Restrict to this tag's competition pool, then softmax across it. Every
        # crop is scored independently, hence dim=-1.
        pool = logits[:, columns]  # (n_crops, n_pool)
        probs = pool.softmax(dim=-1)

        # The tag's score is how much probability mass landed on the positives.
        # pos occupies the first len(pos) columns by construction above.
        positive_mass = probs[:, : len(pos)].sum(dim=-1)  # (n_crops,)

        # Take the best-scoring crop. This is the "max over windows" that lets
        # us find small objects, and the winning crop doubles as our evidence.
        best = int(positive_mass.argmax())
        score = float(positive_mass[best])

        # Absolute-resemblance guard, evaluated on the SAME crop that won.
        positive_logits = logits[best, pos]
        sigmoids = torch.sigmoid(positive_logits)
        strongest = int(sigmoids.argmax())
        sigmoid = float(sigmoids[strongest])
        top_phrase = self._prompt_rows[pos[strongest]]

        low, high, floor = self.thresholds(slug)

        # Order matters: the floor is a veto and is checked FIRST. A high softmax
        # score on an image where nothing actually resembles the tag is an
        # artifact of the pool, not a detection.
        if sigmoid < floor:
            band, present, decided_by = "absent", False, "sigmoid_floor"
        elif score >= high:
            band, present, decided_by = "present", True, "siglip"
        elif score <= low:
            band, present, decided_by = "absent", False, "siglip"
        else:
            band, present, decided_by = "uncertain", None, "siglip"

        return TagVerdict(
            tag=slug,
            present=present,
            score=round(score, 4),
            sigmoid=round(sigmoid, 4),
            band=band,
            confidence=self._confidence(score, low, high, band),
            decided_by=decided_by,
            top_phrase=top_phrase,
            crop=crops[best][1],
        )

    @staticmethod
    def _confidence(score: float, low: float, high: float, band: str) -> str:
        """
        How far past the threshold did we land? Callers use this to decide
        whether a verdict is worth a human review.
        """
        if band == "uncertain":
            return "low"
        margin = score - high if band == "present" else low - score
        if margin >= 0.20:
            return "high"
        if margin >= 0.08:
            return "medium"
        return "low"
