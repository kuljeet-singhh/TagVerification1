"""
Reading and writing `inference/packs.json`.

The file is GENERATED from the `content_tags` table (`dooh export-packs`) and read by the
model tier at import. Nothing hand-edits it any more.

WHY THIS MODULE IS PURE
-----------------------
No database, no FastAPI. Turning a pack file into rows and rows back into a pack file is the
part that can silently corrupt the prompt catalog, so it is testable on its own -- the
round-trip test in tests/test_packs.py needs neither Postgres nor the model.

THE ONE-TIME REFORMAT
---------------------
Exporting does not reproduce the hand-authored file byte for byte, and cannot: the original
carries blank lines between sections, writes `0.30` where json re-emits `0.3`, and keeps
`detect_objects` on one line. Reproducing that needs a bespoke pretty-printer that would
fight every future edit.

What matters is that the reformat is SEMANTICALLY inert -- every prompt, threshold and key
survives -- because `packs_version` hashing the file's bytes means a reformat moves the
fingerprint while moving no score. That distinction is what lets the migration re-stamp
`packs_version_seen` instead of re-running calibration. See docs/TAG_CRUD_IMPLEMENTATION.md
section 3.1.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: Keys `content_tags` models as real columns. Everything else on a tag rides in `extra`.
MODELLED_KEYS = frozenset(
    {"slug", "label", "description", "positives", "negatives", "sigmoid_floor"}
)

#: The comment key carrying the reasoning behind a tag's hard negatives. Handled specially --
#: it is prose a human wrote and the admin UI edits it, so it maps to the `rationale` column.
RATIONALE_KEY = "//negatives"

#: Top-level keys that are NOT tags. They are preserved from the existing file rather than
#: modelled: `$comment` carries the authoring rules, and the rest are pack-wide settings.
FILE_LEVEL_KEYS = ("$comment", "prompt_template", "shared_distractors", "defaults")

#: Emission order for passthrough keys, so an export is byte-stable. JSONB does not preserve
#: key order, so without a canonical order here the file would churn on every export.
_EXTRA_ORDER = ("detect_objects", "escalate", "//sigmoid_floor")


def load_pack(path: Path | str) -> dict[str, Any]:
    return json.loads(Path(path).read_text())


def header_from_pack(pack: dict[str, Any]) -> str:
    """
    The FILE_LEVEL_KEYS of a parsed pack, serialised for the `pack_header.body` column.

    Same serialiser settings as render_pack, so what goes into the database is what comes
    back out of it and straight into an export. The keys are emitted in FILE_LEVEL_KEYS
    order rather than the source file's, which is the same normalisation render_pack already
    applies -- so seeding from a pack and exporting it again cannot move the bytes.
    """
    return json.dumps(
        {k: pack[k] for k in FILE_LEVEL_KEYS if k in pack}, indent=2, ensure_ascii=False
    )


def parse_header(body: str) -> dict[str, Any]:
    """
    `pack_header.body` back into the dict render_pack expects.

    Plain json.loads, and that is the point: Python dicts are insertion-ordered, so the keys
    come back in the order they were stored. This is why the column is TEXT -- JSONB would
    have reordered them in transit, moving packs_version for no reason. See the PackHeader
    docstring.
    """
    return json.loads(body)


def render_pack(file_level: dict[str, Any], tags: list[dict[str, Any]]) -> str:
    """Serialize a pack deterministically. Same input, same bytes, always."""
    out = {k: file_level[k] for k in FILE_LEVEL_KEYS if k in file_level}
    out["tags"] = tags
    return json.dumps(out, indent=2, ensure_ascii=False) + "\n"


def join_rationale(value: Any) -> str | None:
    """
    `//negatives` is a LIST of wrapped prose lines, not a string.

    The admin UI edits one text box, so the list is joined for display. The original list is
    kept verbatim in `extra` as well, so a tag nobody has edited exports exactly as it came
    in -- see tag_to_pack.
    """
    if value is None:
        return None
    if isinstance(value, list):
        return " ".join(str(v).strip() for v in value).strip() or None
    return str(value).strip() or None


def pack_to_row(tag: dict[str, Any], sort_order: int) -> dict[str, Any]:
    """One pack entry -> one `content_tags` row (as a plain dict)."""
    extra = {k: v for k, v in tag.items() if k not in MODELLED_KEYS}
    return {
        "slug": tag["slug"],
        "label": tag["label"],
        "description": tag["description"],
        "positives": list(tag["positives"]),
        "negatives": list(tag["negatives"]),
        "rationale": join_rationale(tag.get(RATIONALE_KEY)),
        "sigmoid_floor": tag.get("sigmoid_floor"),
        "extra": extra,
        "sort_order": sort_order,
        "status": "active",
    }


def row_to_pack(row: dict[str, Any]) -> dict[str, Any]:
    """
    One `content_tags` row -> one pack entry, in the file's key order.

    The rationale is written back as the ORIGINAL list when it has not been edited, and as a
    plain string once it has. That is what keeps an untouched catalog structurally identical
    across a round-trip while still letting an admin rewrite the reasoning in one text box.
    """
    extra = dict(row.get("extra") or {})
    original_rationale = extra.pop(RATIONALE_KEY, None)

    tag: dict[str, Any] = {
        "slug": row["slug"],
        "label": row["label"],
        "description": row["description"],
        "positives": list(row["positives"]),
    }

    rationale = row.get("rationale")
    if rationale:
        unchanged = (
            original_rationale is not None and join_rationale(original_rationale) == rationale
        )
        tag[RATIONALE_KEY] = original_rationale if unchanged else rationale
    elif original_rationale is not None:
        tag[RATIONALE_KEY] = original_rationale

    tag["negatives"] = list(row["negatives"])

    for key in _EXTRA_ORDER:
        if key in extra:
            tag[key] = extra.pop(key)
    for key in sorted(extra):  # anything added to the file later, deterministically ordered
        tag[key] = extra[key]

    if row.get("sigmoid_floor") is not None:
        tag["sigmoid_floor"] = row["sigmoid_floor"]

    return tag
