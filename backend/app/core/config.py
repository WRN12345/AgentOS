"""统一配置：全部从环境变量加载，禁止在代码中硬编码密钥。"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    log_dir: str = "/app/data/logs"

    database_url: str = "postgresql+asyncpg://agentos:agentos@postgres:5432/agentos"
    redis_url: str = "redis://redis:6379/0"

    llm_provider: str = "ollama"
    llm_model: str = ""
    ollama_base_url: str = "http://host.docker.internal:11434"
    openai_compatible_base_url: str = ""
    openai_compatible_api_key: str = ""

    scheduler_example_interval_seconds: float = 60.0


settings = Settings()
