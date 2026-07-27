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
    # 到期/逾期提醒扫描（T3.6，4.2 节）：scheduler 触发周期与"临期"判定窗口
    due_scan_interval_seconds: float = 300.0
    due_soon_horizon_hours: int = 24

    # 认证（第 16 章）：JWT 密钥与令牌有效期；密钥走环境变量，禁止硬编码真实密钥
    jwt_secret: str = "dev-jwt-secret-change-in-production"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 14

    # 初始负责人账号引导（bootstrap，幂等；开发默认值即可，生产必须经环境变量覆盖）
    bootstrap_admin_username: str = "admin"
    bootstrap_admin_password: str = "admin123"
    # 默认项目（首版唯一项目）与初始账号的负责人成员档案
    bootstrap_project_name: str = "AgentOS 项目"
    bootstrap_admin_display_name: str = "项目负责人"


settings = Settings()
