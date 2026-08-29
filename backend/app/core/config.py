"""统一配置：全部从环境变量加载，禁止在代码中硬编码密钥。"""

from typing import Literal

from pydantic import field_validator
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
    # 超时或服务不可达时按此次数重试，耗尽后抛出统一模型错误。
    llm_timeout_seconds: float = 60.0
    llm_max_retries: int = 2
    # 推理模型的 thinking 也计入 `max_tokens`，额度过小会截断结构化 JSON。
    llm_max_tokens: int = 4096

    # memory_chunks 的 PostgreSQL vector 列由迁移 0023 固定为 1024 维。
    # 可切换同为 1024 维的模型；切换模型后需全量重建索引。维度变更需要专门的
    # 数据库迁移，不能仅通过环境变量和重建脚本完成。
    embedding_model: str = "qwen3-embedding:0.6b"
    embedding_dimensions: Literal[1024] = 1024

    @field_validator("embedding_dimensions", mode="before")
    @classmethod
    def _coerce_embedding_dimensions(cls, value: object) -> object:
        # 环境变量值是字符串，而 Literal[1024] 不会自动转换，需在校验前处理。
        if isinstance(value, str) and value.isdigit():
            return int(value)
        return value
    # `openai_compatible` 会把项目文档发送给第三方，界面必须提示数据外发。
    embedding_provider: str = "ollama"
    embedding_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    embedding_api_key: str = ""
    # 超过余弦距离上限视为无相关知识，问答页应拒答并给出线索。
    memory_search_limit: int = 8
    memory_search_max_distance: float = 0.6
    # 文档索引租约：worker 异常退出或任务重投丢失后，超时 indexing 文件自动恢复。
    file_index_lease_seconds: float = 900.0
    file_index_recovery_interval_seconds: float = 60.0

    # Provider 先在线性重试中处理瞬时抖动；耗尽后再按运行粒度指数退避重投。
    agent_run_max_retries: int = 3
    agent_run_retry_base_seconds: float = 30.0

    scheduler_example_interval_seconds: float = 60.0
    # 到期提醒的调度周期与临期判定窗口。
    due_scan_interval_seconds: float = 300.0
    due_soon_horizon_hours: int = 24
    # Workflow Risk Agent 默认每 24 小时扫描一次。
    agent_risk_scan_interval_seconds: float = 86400.0
    # 核心记忆提议默认每 24 小时扫描一次。
    memory_proposal_expire_interval_seconds: float = 86400.0

    # JWT 密钥必须由生产环境变量覆盖，禁止硬编码真实密钥。
    jwt_secret: str = "dev-jwt-secret-change-in-production"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 14

    # bootstrap 可重复执行；生产环境必须覆盖默认管理员凭据。
    bootstrap_admin_username: str = "admin"
    bootstrap_admin_password: str = "admin123"
    # 默认项目和初始管理员展示名称；项目负责人由管理员另行创建。
    bootstrap_project_name: str = "AgentOS 项目"
    bootstrap_admin_display_name: str = "管理员"

    # 数据库仅保存相对 `storage_key`，上传目录不得直接暴露。
    storage_backend: str = "local"
    storage_root: str = "/app/data/uploads"
    upload_max_bytes: int = 20 * 1024 * 1024
    # 上传白名单（逗号分隔）：扩展名（小写、带点）与声明的 MIME 类型
    # `.docx` 用于记忆模块的 Word 知识文档。
    upload_allowed_extensions: str = ".txt,.md,.csv,.json,.pdf,.png,.jpg,.jpeg,.zip,.docx"
    upload_allowed_mime_types: str = (
        "text/plain,text/markdown,text/csv,application/json,"
        "application/pdf,image/png,image/jpeg,application/zip,"
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )

    @property
    def allowed_upload_extensions(self) -> frozenset[str]:
        return frozenset(
            e.strip().lower() for e in self.upload_allowed_extensions.split(",") if e.strip()
        )

    @property
    def allowed_upload_mime_types(self) -> frozenset[str]:
        return frozenset(m.strip() for m in self.upload_allowed_mime_types.split(",") if m.strip())

    @property
    def llm_is_external(self) -> bool:
        """当前模型 Provider 是否会向外部服务发送数据。"""
        return self.llm_provider == "openai_compatible"


settings = Settings()
