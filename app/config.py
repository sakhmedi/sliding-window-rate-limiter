"""Настройки приложения из переменных окружения (и файла .env)."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Настройки сервиса.

    Имена полей совпадают с переменными окружения без учёта регистра:
    `rate_limit_requests` <- RATE_LIMIT_REQUESTS и т.д.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # N — сколько запросов разрешено за окно.
    rate_limit_requests: int = Field(default=10, gt=0)
    # T — длина скользящего окна в секундах.
    rate_limit_window_seconds: int = Field(default=60, gt=0)
    # Адрес Redis, где хранится состояние лимитера.
    redis_url: str = "redis://localhost:6379/0"
