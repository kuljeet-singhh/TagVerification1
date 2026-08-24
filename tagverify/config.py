"""
Configuration, read once at import from the environment (and `.env` in development).

The old code reached for `process.env.X` at seven different call sites, which meant
"is this variable required?" could only be answered by grepping. Collecting them here
makes the answer a type: `str` is required, `str | None` is optional, and the one
genuinely-optional-but-important case (INFERENCE_URL) gets a comment saying why.

Nothing here raises on a missing value. A missing DATABASE_URL must degrade to a
`/health` response that reports the problem, not to a process that refuses to boot —
monitoring you cannot reach is not monitoring.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # .env.local is read too, so an existing checkout keeps working unchanged.
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str | None = None

    # Inference target. HF_SPACE wins when both are set; INFERENCE_URL is the local-dev
    # escape hatch that lets you run `python inference/app.py` without deploying.
    hf_space: str | None = None
    inference_url: str | None = None
    hf_token: str | None = None

    # Absent means /admin refuses access rather than opening it. See tagverify/auth/admin.py.
    admin_password: str | None = None

    log_level: str = "INFO"

    # The playground carries no API key by design (see tagverify/web/playground.py), so it is
    # limited per-IP instead. Generous enough for real use, low enough that a public URL
    # is not free inference.
    playground_rate_limit_per_min: int = 20
    admin_login_attempts_per_min: int = 5

    @property
    def inference_target(self) -> str | None:
        """Where to reach the inference service, or None if neither is configured."""
        return (self.hf_space or "").strip() or (self.inference_url or "").strip() or None


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings()
