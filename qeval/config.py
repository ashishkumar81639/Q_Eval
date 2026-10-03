"""Typed settings read once from the environment (and `.env` if present)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    qeval_token: SecretStr | None = Field(default=None, alias="QEVAL_TOKEN")
    anthropic_api_key: SecretStr | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    base_url: str = Field(default="https://qloop-eval-api.onrender.com", alias="QEVAL_BASE_URL")
    database_url: str | None = Field(default=None, alias="DATABASE_URL")
    judge_model: str = Field(default="claude-sonnet-5-5", alias="JUDGE_MODEL")
    judge_effort: str = Field(default="medium", alias="JUDGE_EFFORT")
    sim_model: str = Field(default="claude-sonnet-5-5", alias="SIM_MODEL")
    sim_effort: str = Field(default="low", alias="SIM_EFFORT")
    concurrency: int = Field(default=4, alias="QEVAL_CONCURRENCY")
    min_send_interval_ms: int = Field(default=500, alias="QEVAL_MIN_SEND_INTERVAL_MS")
    max_agent_messages: int = Field(default=1500, alias="QEVAL_MAX_AGENT_MESSAGES")
    max_judge_tokens: int = Field(default=3_000_000, alias="QEVAL_MAX_JUDGE_TOKENS")
    state_each_turn: bool = Field(default=True, alias="QEVAL_STATE_EACH_TURN")
    runs_dir: Path = Field(default=ROOT / "runs", alias="QEVAL_RUNS_DIR")

    def require_token(self) -> str:
        if self.qeval_token is None or not self.qeval_token.get_secret_value():
            raise SystemExit("QEVAL_TOKEN is not set")
        return self.qeval_token.get_secret_value()

    def require_anthropic_key(self) -> str:
        if self.anthropic_api_key is None or not self.anthropic_api_key.get_secret_value():
            raise SystemExit("ANTHROPIC_API_KEY is not set (needed for the judge and simulated user)")
        return self.anthropic_api_key.get_secret_value()

    def public_dict(self) -> dict:
        """Settings safe to store with a run (no secrets)."""
        d = self.model_dump(exclude={"qeval_token", "anthropic_api_key"})
        d["runs_dir"] = str(d["runs_dir"])
        if d.get("database_url"):
            d["database_url"] = d["database_url"].split("@")[-1]
        return d


@lru_cache
def get_settings() -> Settings:
    return Settings()
