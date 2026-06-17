"""Bootstrap configuration loaded from environment / .env.

Only secrets and paths needed *before* the SQLite DB exists live here. All runtime
configuration (schedule, excluded accounts, topics, digest style) is stored in the DB
and editable through the web UI — see db/models.py.
"""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Ollama (local LLM)
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "gemma4:e4b"

    # SMTP delivery
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    email_from: str = ""
    email_to: str = ""

    # Telegram delivery
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Paths
    data_dir: str = "data"
    db_path: str = "data/agent.db"
    storage_state_path: str = "auth/storage_state.json"

    @property
    def data_path(self) -> Path:
        return Path(self.data_dir)

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.db_path}"


settings = Settings()
