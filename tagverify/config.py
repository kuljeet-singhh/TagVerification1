"""
Configuration, read once at import from the environment (and `.env` in development).

The old code reached for `process.env.X` at seven different call sites, which meant
"is this variable required?" could only be answered by grepping. Collecting them here
makes the answer a type: `str` is required, `str | None` is optional, and the one
genuinely-optional-but-important case gets a comment saying why.

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

    # Which scorer decides a verdict.
    #
    #   vlm     a vision language model, read per request (tagverify/scoring/vlm.py)
    #   fake    canned verdicts from a fixture (tagverify/scoring/fake.py) -- offline
    #           development and tests only, never a deployment
    #
    # "siglip" was the third value and is gone: the ranking model, its Space, its prompt pack
    # and the publish step that carried one to the other were removed once a reading model
    # made all four unnecessary. It is preserved in the original repository
    # (kuljeet-singhh/Dooh-TagVerefication, `main` at d33d463) rather than here, and the
    # comparison that justified the removal is in inference/gate_cache.json.
    scorer: str = "vlm"


    # The VLM scorer. Provider and model are config, not code, because docs/VLM_SCORING.md 6.1
    # makes them a swappable detail: the argument is for a model that READS its prompt per
    # request, and which one does the reading is settled by the eval harness rather than by
    # preference. Both providers share the prompt, the schema and every failure rule -- only
    # the SDK call differs -- so a switch is one env var and the numbers stay comparable.
    #
    #   anthropic  tagverify/scoring/vlm.py     needs ANTHROPIC_API_KEY
    #   gemini     tagverify/scoring/gemini.py  needs GEMINI_API_KEY
    #
    # VLM_MODEL is NOT defaulted per provider on purpose: a model id that silently does not
    # match the provider is the kind of mistake that shows up as a bill rather than an error.
    # Set both together. Suggested pairs: claude-opus-5 / gemini-3.7-flash, and locally
    # claude-haiku-4-5 or gemini-2.5-flash-lite -- development traffic does not need the top
    # tier, and every playground click is billed.
    vlm_provider: str = "anthropic"
    vlm_model: str = "claude-opus-5"
    anthropic_api_key: str | None = None
    gemini_api_key: str | None = None

    # Absent means /admin refuses access rather than opening it. See tagverify/auth/admin.py.
    admin_password: str | None = None


    log_level: str = "INFO"

    # The playground carries no API key by design (see tagverify/web/playground.py), so it is
    # limited per-IP instead. Generous enough for real use, low enough that a public URL
    # is not free inference.
    playground_rate_limit_per_min: int = 20
    admin_login_attempts_per_min: int = 5

    # How many reverse proxies sit in front of this process. Both limits above are per-IP,
    # and behind a proxy every caller arrives from the PROXY's address -- so the playground
    # budget becomes one bucket for the whole internet, and the admin lockout inverts into a
    # denial of service: five wrong passwords from any visitor would lock every admin out.
    #
    # 0 -- the default -- reads the socket peer and ignores X-Forwarded-For entirely, which
    # is what a direct bind and `make serve-lan` want; on a LAN any device can forge that
    # header, and the README says so. Set it to 1 on a PaaS that terminates TLS in front.
    # See client_ip() in tagverify/auth/deps.py for which entry it reads and why.
    trust_proxy_hops: int = 0

    @property
    def scorer_target(self) -> str | None:
        """
        What the ACTIVE scorer needs in order to work, or None if it is not configured.

        The same question `inference_target` answers, asked of whichever scorer is switched
        on, so `/api/v1/health` and `dooh health` keep reporting a real answer rather than
        one that is only true of SigLIP. An unknown `SCORER` value reports None -- not
        configured -- rather than falling back to a scorer nobody asked for: silently
        scoring with something other than what was requested is the sort of thing that gets
        noticed a month later.
        """
        match (self.scorer or "").strip().lower():
            case "vlm":
                key = (
                    self.gemini_api_key
                    if self.vlm_provider.strip().lower() == "gemini"
                    else self.anthropic_api_key
                )
                return (key or "").strip() and f"{self.vlm_provider}:{self.vlm_model}" or None
            case "fake":
                return "fake"
            case _:
                return None


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings()
