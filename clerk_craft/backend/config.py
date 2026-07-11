"""Конфигурация приложения на основе переменных окружения (.env)."""
import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


# Корень проекта: <repo>/clerk_craft/.env
# config.py лежит в <repo>/clerk_craft/backend/config.py → вверх на два уровня
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    """Настройки приложения."""

    ROUTER_API_KEY: str
    ROUTER_BASE_URL: str = "https://api.aitunnel.ru/v1"
    MODEL_LLM_1: str = "deepseek-v4-flash"
    MODEL_LLM_2: str = "minimax-m3"

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
