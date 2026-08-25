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
    # 模型调用超时与失败重试（17.3 节；超时/不可达按此次数重试后抛统一封装错误）
    llm_timeout_seconds: float = 60.0
    llm_max_retries: int = 2
    # 单次生成的最大 token 数（OpenAI 兼容 Provider 发送 max_tokens）。
    # 推理模型的 thinking 也占用该额度，默认太小会导致 JSON 输出被截断。
    llm_max_tokens: int = 4096

    # memory_chunks 的 PostgreSQL vector 列由迁移 0023 固定为 1024 维。
    # 可切换同为 1024 维的模型；切换模型后需全量重建索引。维度变更需要专门的
    # 数据库迁移，不能仅通过环境变量和重建脚本完成。
    embedding_model: str = "qwen3-embedding:0.6b"
    embedding_dimensions: Literal[1024] = 1024

    @field_validator("embedding_dimensions", mode="before")
    @classmethod
    def _coerce_embedding_dimensions(cls, value: object) -> object:
        # 环境变量（docker-compose 注入的 EMBEDDING_DIMENSIONS）总是字符串，
        # Literal[1024] 不会自动把 "1024" 转成 int，不 coerce 会直接启动失败。
        if isinstance(value, str) and value.isdigit():
            return int(value)
        return value
    # embedding Provider 切换：默认 ollama（本地）；openai_compatible 接智谱等
    # OpenAI 兼容云端服务——此时项目文档内容将发送至第三方（16 节数据外发提示）
    embedding_provider: str = "ollama"
    embedding_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    embedding_api_key: str = ""
    # 检索（设计文档第 5 节、16.13）：默认返回条数与余弦距离上限
    # （超过上限视为"知识库没有相关内容"，问答页拒答并给线索）
    memory_search_limit: int = 8
    memory_search_max_distance: float = 0.6
    # 文档索引租约：worker 异常退出或任务重投丢失后，超时 indexing 文件自动恢复。
    file_index_lease_seconds: float = 900.0
    file_index_recovery_interval_seconds: float = 60.0

    # Agent 运行级失败重试（17.3 节，T5.6）：指数退避，间隔 = base * 2^attempt。
    # 与 provider 层 LLM_MAX_RETRIES（单次调用内的线性重试，应对瞬时抖动）是
    # 两道不同防线：provider 重试耗尽后错误冒泡到本层，按运行粒度退避重投。
    agent_run_max_retries: int = 3
    agent_run_retry_base_seconds: float = 30.0

    scheduler_example_interval_seconds: float = 60.0
    # 到期/逾期提醒扫描（T3.6，4.2 节）：scheduler 触发周期与"临期"判定窗口
    due_scan_interval_seconds: float = 300.0
    due_soon_horizon_hours: int = 24
    # Workflow Risk Agent 周期风险扫描（T5.5，4.2 节逾期风险扫描）触发周期，默认 24 小时
    agent_risk_scan_interval_seconds: float = 86400.0
    # 核心记忆提议过期扫描（M4.5，16.6）触发周期，默认 24 小时
    memory_proposal_expire_interval_seconds: float = 86400.0

    # 认证（第 16 章）：JWT 密钥与令牌有效期；密钥走环境变量，禁止硬编码真实密钥
    jwt_secret: str = "dev-jwt-secret-change-in-production"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 14

    # 初始管理员账号引导（bootstrap，幂等；开发默认值即可，生产必须经环境变量覆盖）
    bootstrap_admin_username: str = "admin"
    bootstrap_admin_password: str = "admin123"
    # 默认项目（首版唯一项目）与初始账号的管理员成员档案（负责人由管理员另行创建）
    bootstrap_project_name: str = "AgentOS 项目"
    bootstrap_admin_display_name: str = "管理员"

    # 文件存储（第 14 章）：数据库仅存相对 storage_key，上传目录禁止直接暴露
    storage_backend: str = "local"
    storage_root: str = "/app/data/uploads"
    upload_max_bytes: int = 20 * 1024 * 1024
    # 上传白名单（逗号分隔）：扩展名（小写、带点）与声明的 MIME 类型
    # .docx 为记忆模块知识文档支持的 Word 格式（设计文档第 4 节）
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
        """当前模型 Provider 是否为外部（云端）服务。

        T5.7 前端据此提示"数据将发送至外部服务"（16 节）；
        与 ModelProvider.is_external 保持一致。
        """
        return self.llm_provider == "openai_compatible"


settings = Settings()
