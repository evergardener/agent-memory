from functools import lru_cache
from typing import Literal
from uuid import UUID

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENT_MEMORY_", env_file=".env", extra="ignore")

    database_url: str = "postgresql://agent_memory:agent_memory@127.0.0.1:5432/agent_memory"
    service_token: SecretStr = Field(min_length=16)
    namespace: str = "hermes:user-primary"
    log_level: str = "INFO"
    worker_poll_seconds: float = Field(default=0.5, gt=0, le=60)
    worker_lease_seconds: int = Field(default=180, ge=5, le=3600)
    worker_role: Literal["core", "model"] = "core"
    vault_root_key_file: str = "/run/secrets/vault_root_key"
    ui_password_hash: str = ""
    ui_session_secret: SecretStr = Field(min_length=32)
    report_interval_days: int = Field(default=7, ge=1, le=365)
    candidate_retention_days: int = Field(default=30, ge=1, le=3650)
    current_state_days: int = Field(default=7, ge=1, le=365)
    weather_state_hours: int = Field(default=24, ge=1, le=720)
    continuity_days: int = Field(default=14, ge=1, le=365)
    stage_dormant_days: int = Field(default=90, ge=1, le=3650)
    stage_forget_days: int = Field(default=365, ge=1, le=10000)
    model_enabled: bool = False
    model_name: str = ""
    model_api_base: str = ""
    model_api_key: SecretStr = SecretStr("")
    model_timeout_seconds: float = Field(default=30, gt=0, le=300)
    model_max_retries: int = Field(default=2, ge=0, le=10)
    model_backfill_batch_size: int = Field(default=25, ge=1, le=500)
    model_max_atomic_facts: int = Field(default=8, ge=1, le=20)
    model_allow_external_data: bool = False
    model_evaluation_mode: bool = False
    model_evaluation_plan_sha: str = ""
    model_evaluation_turn_allowlist: str = ""
    trusted_observation_tool_allowlist: str = "terminal,exec,execute_code,shell,health_probe"

    @property
    def trusted_observation_tools(self) -> frozenset[str]:
        return frozenset(
            item.strip().casefold()
            for item in self.trusted_observation_tool_allowlist.split(",")
            if item.strip()
        )

    @property
    def model_evaluation_turn_ids(self) -> tuple[UUID, ...]:
        values = {
            UUID(item.strip())
            for item in self.model_evaluation_turn_allowlist.split(",")
            if item.strip()
        }
        return tuple(sorted(values, key=str))


@lru_cache
def get_settings() -> Settings:
    return Settings()
