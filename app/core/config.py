"""
Centralized, typed application configuration.

Why this file exists: every other module that needs an environment value
(DATABASE_URL, MESH_API_KEY, etc.) imports `settings` from here instead of
calling `os.environ` directly. That gives us one place that knows how to
load `.env`, one place to validate that required values are present, and
one place a reviewer can check to confirm nothing is hardcoded.

Nothing in this file is used yet — Stage 1 only proves the settings load
correctly and the app boots with them. `database_url`, `mesh_api_key`, etc.
become load-bearing starting Stage 2 and Stage 9 respectively. They're
declared now because `.env.example` has to document the full environment
surface a developer will eventually need, and because pydantic-settings
gives us free validation (e.g. malformed values fail fast at startup
rather than surfacing as a confusing error three stages from now).
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # General
    app_env: str = "development"
    app_name: str = "ChennAI Labs"
    log_level: str = "INFO"

    # Session / security (Stage 2+)
    session_secret: str = "dev-only-insecure-secret-change-me"
    session_ttl_days: int = 7  # how long a login stays valid before re-auth is required

    # Database (Stage 2+)
    database_url: str = "sqlite:///./chennai_labs.db"

    # Vector store (Stage 4+)
    vector_db_path: str = "./.chroma"

    # Mesh API (Stage 9+)
    mesh_api_key: str = ""
    mesh_base_url: str = "https://api.meshapi.ai/v1"
    mesh_embedding_model: str = "mesh-embed-v1"
    mesh_chat_model: str = "mesh-chat-v1"

    # Proactive digest (Stage 15) — an APScheduler job, not Celery (Stage
    # 0, Section 15: nothing here needs distributed queuing). Disabled by
    # default in the test settings via tests/conftest.py's autouse
    # fixture, so pytest never spins up a real background thread.
    digest_enabled: bool = True
    digest_hour_utc: int = 6  # when the daily digest runs, UTC, 0-23

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache
def get_settings() -> Settings:
    """
    Cached so we parse the environment once per process, not once per
    request. FastAPI routes that need config should depend on this function
    rather than importing a module-level `settings` singleton directly —
    it makes overriding config in tests a one-line dependency override
    instead of monkeypatching a global.
    """
    return Settings()


settings = get_settings()
