from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    database_url: str = "sqlite+pysqlite:///./local.db"
    cors_origins: str = "http://localhost:5173"
    upload_dir: Path = Path("./uploads")
    max_upload_mb: int = 20
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()
