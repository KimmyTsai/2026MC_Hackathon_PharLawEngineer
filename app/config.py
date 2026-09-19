"""Environment configuration and run-mode switching."""

from __future__ import annotations

from enum import Enum
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent
TAIPEI = ZoneInfo("Asia/Taipei")


class RunMode(str, Enum):
    """`replay` drives everything from data/scenarios; `live` calls real providers."""

    replay = "replay"
    live = "live"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    mode: RunMode = RunMode.replay
    scenario: str = "demo_wed"
    replay_speed: float = 1.0
    port: int = 3000

    gemini_api_key: str | None = None
    gemini_flash_model: str = "gemini-3-flash-preview"
    gemini_pro_model: str | None = None

    cwa_api_key: str | None = None
    tdx_client_id: str | None = None
    tdx_client_secret: str | None = None

    use_gemma: bool = False
    ollama_model: str | None = None
    ollama_host: str = "http://127.0.0.1:11434"

    data_dir: Path = Field(default=REPO_ROOT / "data")
    web_dir: Path = Field(default=REPO_ROOT / "web")

    @property
    def scenario_path(self) -> Path:
        return self.data_dir / "scenarios" / f"{self.scenario}.json"

    @property
    def graph_path(self) -> Path:
        return self.data_dir / "campus_graph.json"

    @property
    def rooms_path(self) -> Path:
        return self.data_dir / "rooms.json"

    @property
    def mailbox_dir(self) -> Path:
        return self.data_dir / "mailbox"

    @property
    def outbox_dir(self) -> Path:
        return self.data_dir / "outbox"

    def reasoning_model(self) -> str:
        """Pro when configured, otherwise Flash. Never guess an unverified model ID."""
        return self.gemini_pro_model or self.gemini_flash_model


@lru_cache
def get_settings() -> Settings:
    return Settings()
