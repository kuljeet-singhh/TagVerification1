"""
API key issue / revoke.

The key is never stored. We keep sha256(key) and look up by that, so a database leak does not
hand out working credentials. Because lookup is an exact match on an indexed hash, there is
no string comparison to time-attack and no need for a constant-time compare.

sha256 (not bcrypt/argon2) is the right choice here specifically because the key is 32 bytes
of CSPRNG output, not a human-chosen password. There is no dictionary to attack, so a slow
KDF would only add latency to every single API request.

NOTE: request-time verification lives in tagverify/auth/authenticate.py, not here. There is
deliberately no `verify_api_key()` in this file. An authenticate-only helper that does not
also charge the rate limit is a footgun: the next person to add a route would reach for it
and ship an unthrottled endpoint without noticing. `authenticate_and_charge()` is the single
door in, and it does both in one query.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from tagverify.db.models import ApiKey

KEY_PREFIX = "dooh_live_"
SECRET_BYTES = 32
DISPLAY_CHARS = 8


@dataclass(slots=True)
class IssuedKey:
    #: Shown to the user exactly once. Not recoverable afterwards.
    plaintext: str
    id: str
    key_hash: str
    key_prefix: str


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _base64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def generate_api_key() -> IssuedKey:
    """Create a key. The caller persists it and shows the plaintext once."""
    secret = _base64url(secrets.token_bytes(SECRET_BYTES))
    plaintext = f"{KEY_PREFIX}{secret}"
    return IssuedKey(
        plaintext=plaintext,
        id=str(uuid.uuid4()),
        key_hash=sha256_hex(plaintext),
        key_prefix=secret[:DISPLAY_CHARS],
    )


async def create_api_key(
    session: AsyncSession,
    name: str,
    rate_limit_per_min: int = 60,
    scopes: Sequence[str] = (),
) -> IssuedKey:
    """
    Issue a key and persist it. Surface the plaintext once, then discard it.

    `scopes` defaults to empty, which is analyze-only and what almost every key should be.
    Pass TAGS_WRITE only for a key whose job is editing the catalog, and issue it SEPARATELY
    from the analyze key — a single key that can both score images and redefine what the
    scores mean is the thing scopes exist to avoid.
    """
    key = generate_api_key()
    session.add(
        ApiKey(
            id=key.id,
            name=name,
            key_hash=key.key_hash,
            key_prefix=key.key_prefix,
            rate_limit_per_min=rate_limit_per_min,
            # Sorted and de-duplicated so the stored value is canonical: the admin panel
            # renders it directly, and two keys with the same powers should read the same.
            scopes=sorted(set(scopes)),
        )
    )
    await session.flush()
    return key


async def revoke_api_key(session: AsyncSession, key_id: str) -> None:
    """Revoke, never hard-delete — the audit rows in `analyses` reference this row."""
    await session.execute(
        update(ApiKey).where(ApiKey.id == key_id).values(revoked_at=datetime.now(UTC))
    )


def masked_key(key_prefix: str) -> str:
    """For display: "dooh_live_a1b2c3d4…" """
    return f"{KEY_PREFIX}{key_prefix}…"
