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
    # Populated from the key file; one key per line. Never logged.
    gemini_api_keys: list[str] = Field(default_factory=list, exclude=True)
    gemini_key_index: int = 0
    gemini_flash_model: str = "gemini-3-flash-preview"
    gemini_pro_model: str | None = None

    # Maps needs a *standard* key (AIza...), Gemini needs an *authentication*
    # key (AQ....). Google does not let one key serve both, so they live apart:
    # GEMINI_API_KEY.txt holds Gemini keys, MAPS_API_KEY.txt holds the Maps one.
    maps_api_key: str | None = None

    cwa_api_key: str | None = None
    tdx_client_id: str | None = None
    tdx_client_secret: str | None = None

    # cache = replay a recorded turn, else call and record (free tier is 20
    # requests per day per model). live = always call. replay = cassette only.
    # off = never call a model; the deterministic planner decides.
    agent_mode: str = "cache"
    cassette: str = "demo_wed"
    # Waiting out a per-minute quota is right when recording, wrong in a web
    # request. scripts/record_cassette.py raises this.
    quota_retries: int = 0

    use_gemma: bool = False
    ollama_model: str | None = None
    ollama_host: str = "http://127.0.0.1:11434"

    data_dir: Path = Field(default=REPO_ROOT / "data")
    web_dir: Path = Field(default=REPO_ROOT / "web")

    def model_post_init(self, _context: object) -> None:
        """Fall back to a git-ignored key file when no env var is set.

        Mirrors what scripts/test-ai-studio.mjs does, so a teammate who dropped
        the key in a file instead of .env still gets a working app.

        The file may hold several keys, one per line: the free tier is limited
        per project, so a key from a second project is a second daily quota.
        `GEMINI_KEY_INDEX` picks one. Never log or echo the values.
        """
        keys = list(self.gemini_api_keys) if self.gemini_api_keys else []
        if not keys:
            for name in ("API", "API.txt", "GEMINI_API_KEY.txt"):
                candidate = REPO_ROOT / name
                if not candidate.exists():
                    continue
                keys = [
                    line.strip()
                    for line in candidate.read_text(encoding="utf-8-sig").splitlines()
                    if line.strip() and not line.lstrip().startswith("#")
                ]
                if keys:
                    break

        if self.gemini_api_key and self.gemini_api_key not in keys:
            keys.insert(0, self.gemini_api_key)
        object.__setattr__(self, "gemini_api_keys", keys)

        if keys:
            index = min(max(self.gemini_key_index, 0), len(keys) - 1)
            object.__setattr__(self, "gemini_api_key", keys[index])

        if not self.maps_api_key:
            for name in ("MAPS_API_KEY", "MAPS_API_KEY.txt"):
                candidate = REPO_ROOT / name
                if candidate.exists():
                    value = candidate.read_text(encoding="utf-8-sig").strip()
                    if value:
                        object.__setattr__(self, "maps_api_key", value.splitlines()[0].strip())
                        break

    @property
    def has_maps(self) -> bool:
        return bool(self.maps_api_key)

    def gemini_key_warnings(self) -> list[str]:
        """Standard keys cannot call the Gemini API, whatever the restrictions
        say. Flag one sitting in the Gemini key file instead of letting it fail
        at request time."""
        return [
            f"GEMINI_API_KEY 第 {i + 1} 把看起來是 Maps 標準金鑰（AIza…），"
            "Gemini 需要 AI Studio 的驗證金鑰；Maps 金鑰請放 MAPS_API_KEY.txt"
            for i, key in enumerate(self.gemini_api_keys)
            if key.startswith("AIza")
        ]

    @property
    def cassette_path(self) -> Path:
        return self.data_dir / "agent_cassettes" / f"{self.cassette}.json"

    @property
    def has_gemini(self) -> bool:
        return bool(self.gemini_api_key)

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
