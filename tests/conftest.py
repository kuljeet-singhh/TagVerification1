"""
Shared fixtures.

Tests run against the ASGI app in-process via httpx.ASGITransport, so no server has to be
started and no port has to be free. Anything that needs the database or the inference service
is marked and skipped when they are not configured, so `pytest` is always runnable — a test
suite you can only run with production credentials to hand is a test suite nobody runs.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from tagverify import config as _config

ROOT = Path(__file__).resolve().parent.parent

# Must be set before tagverify.config is first imported.
os.environ.setdefault("ADMIN_PASSWORD", "test-admin-password")

# PIN THE SCORER. Not setdefault -- an unconditional set, because a real environment variable
# outranks .env/.env.local and that is the only way to stop the developer's own configuration
# deciding what the suite tests.
#
# This is not hypothetical tidiness. Adding SCORER=vlm to a .env.local turned ten tests in
# test_content_tags.py red on one machine and left them green on another: they exercise
# publish_state, which compares the catalog against the running Space and is meaningless once
# a VLM is scoring, so the code correctly stops asking -- and the tests correctly noticed.
# A suite whose result depends on an untracked file is not a suite.
#
# Tests that want a VLM say so explicitly (see tests/test_vlm.py, which monkeypatches
# scoring.registry.name), which is the right way round: the default is the shipped default.
os.environ["SCORER"] = "siglip"


@pytest.fixture(scope="session")
def admin_password() -> str:
    # Whatever the app itself is using — .env.local usually supplies a real one, and the
    # setdefault above only covers a checkout that has none.
    return _config.settings().admin_password or ""


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    from tagverify.main import app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=60
    ) as c:
        yield c


# Read through Settings, not os.environ: the values normally come from .env / .env.local,
# which pydantic-settings loads but never exports to the process environment. Checking
# os.environ here would skip the whole suite on a perfectly well-configured checkout.
_settings = _config.settings()

needs_db = pytest.mark.skipif(
    not (_settings.database_url or "").strip(), reason="DATABASE_URL is not set"
)
needs_inference = pytest.mark.skipif(
    not _settings.inference_target, reason="neither HF_SPACE nor INFERENCE_URL is set"
)


@pytest.fixture
async def api_key() -> AsyncIterator[str]:
    """
    A real key in the real database, DELETED afterwards.

    Deleted rather than revoked, even though revoking is what production does. A revoking
    fixture leaves a row behind on every single run, and the keys table is a screen a human
    reads — a few hundred dead `pytest` rows buried the three keys that actually matter.

    Deletion is safe here: `analyses.api_key_id` is ON DELETE SET NULL, so any audit rows
    this test happened to write survive, just without attribution to a key that no longer
    exists. That is the correct outcome for test traffic. Revocation is still exercised
    directly by tests/test_admin.py.
    """
    from sqlalchemy import delete

    from tagverify.auth.keys import create_api_key
    from tagverify.db.models import ApiKey
    from tagverify.db.session import session_scope

    async with session_scope() as session:
        issued = await create_api_key(session, "pytest", rate_limit_per_min=10_000)

    yield issued.plaintext

    async with session_scope() as session:
        await session.execute(delete(ApiKey).where(ApiKey.id == issued.id))


@pytest.fixture(scope="session")
def beer_image() -> bytes:
    return next((ROOT / "inference/eval/alcohol/pos").glob("*.jpg")).read_bytes()


@pytest.fixture(scope="session")
def juice_image() -> bytes:
    """A near-miss: another beverage that must NOT read as alcohol."""
    return (ROOT / "inference/eval/alcohol/neg/Soft_drink.jpg").read_bytes()


@pytest.fixture(scope="session")
def beer_video() -> bytes:
    """
    A clip where the beer appears in ONE scene of three.

    It is the case the whole video path exists for: a first-frame check would miss it
    entirely. The encoder lives in tests/support/videos.py — it used to be written out here, in
    test_video.py and inline in test_api.py, three copies with different sizes.
    """
    from PIL import Image

    from tests.support.videos import SIZE, middle_scene_clip, neutral_image

    beer = Image.open(next((ROOT / "inference/eval/alcohol/pos").glob("*.jpg")))
    return middle_scene_clip(beer, neutral_image(), size=SIZE)
