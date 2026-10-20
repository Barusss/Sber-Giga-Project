from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Reading the repository root .env works when running Python from backend/.
# Docker Compose also injects variables through env_file: .env.
REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    database_url: str = "sqlite+pysqlite:///./local.db"
    cors_origins: str = "http://localhost:5173"
    upload_dir: Path = Path("./uploads")
    max_upload_mb: int = 20

    # SecretStr redacts the key in Settings repr and error messages.
    deepseek_api_key: SecretStr = SecretStr("")
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-flash"
    deepseek_thinking: Literal["enabled", "disabled"] = "disabled"
    deepseek_timeout_seconds: float = Field(default=60.0, gt=0)
    deepseek_max_tokens: int = Field(default=3000, ge=1)

    # Optional local semantic channel; without the model the analysis uses lexical search.
    embedding_enabled: bool = False
    embedding_model: str = "intfloat/multilingual-e5-small"
    embedding_batch_size: int = 32

    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env", Path(".env")),
        extra="ignore",
    )


settings = Settings()
