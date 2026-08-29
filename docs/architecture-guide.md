# AgentOS 架构指南

> 本文档整合自原 phase-1 至 phase-6 开发者指南，反映当前已落地的架构。原分阶段指南可通过 git 历史追溯。本文档定位为长期参考——聚焦架构形态、模块职责、关键约定与设计原则，不包含分阶段施工步骤与命令清单。文中章节号（如 4.1、17.2）均指设计文档 `docs/2026-07-26-agentos-workflow-platform-design.md`。

## 1. 总览

### 1.1 模块化单体架构理念

AgentOS 采用"模块化单体 + 后台进程分离"架构（设计文档 4.1、4.2 节）：

- **后端是一个 FastAPI 单体**，按业务领域拆分子包（`backend/app/domains/`），权限策略集中在应用服务层，避免散落到路由或 ORM。
- **业务状态以 PostgreSQL 为唯一权威**（原则 1）。Redis 仅承担队列、心跳、SSE 事件通道等横切技术机制，不存业务状态。
- **API 进程是唯一可鉴权、可审计的业务变更入口**。Worker / Scheduler / Agent 子系统均不直接修改正式业务状态——这是 4.2 节的硬约束，靠"不引入写业务命令代码路径"落实。
- **Agent 只生成 `agent_suggestions` 记录**，不具备任何写业务状态的工具（原则 2：人类决定，Agent 建议）。状态变更永远由人工在前端调用正式 REST 命令完成。

### 1.2 六服务编排

仓库根 `docker-compose.yml` 编排六个服务（19.2 节）：

| 服务 | 角色 | 镜像 | 进程 |
|---|---|---|---|
| `frontend` | Web 入口，nginx 提供静态资源并反代 `/api/` | `agentos-frontend` | nginx |
| `backend` | FastAPI 模块化单体（4.1），唯一对外 API | `agentos-backend` | `alembic upgrade head && python -m app.scripts.bootstrap && uvicorn` |
| `worker` | 后台任务消费者（4.2），与 API 共用代码 | `agentos-worker` | `python -m app.workers.worker` |
| `scheduler` | 周期任务触发器（4.2） | `agentos-scheduler` | `python -m app.workers.scheduler` |
| `postgres` | 主数据库（第 11 章） | `pgvector/pgvector:pg16` | postgres |
| `redis` | 任务队列 + 心跳媒介（4.2） | `redis:7-alpine` | redis-server（AOF 开启） |

要点：

- **进程边界**：backend / worker / scheduler 是三个独立容器，但构建自同一个 `backend/` 镜像（`backend/Dockerfile`），仅启动命令不同。这是 4.2 节"Worker 与 API 使用同一套领域模型和数据库访问层，但运行在独立进程中"的落地方式。
- **启动顺序基于健康检查**：postgres、redis 自带 healthcheck；backend / worker / scheduler 通过 `depends_on: condition: service_healthy` 等待两者就绪后才启动；frontend 只等 backend `service_started`。
- **Ollama 不在 Compose 内**（19.2）：LLM 运行在宿主机，backend / worker / scheduler 通过 `host.docker.internal:11434` 访问（Linux 下需 `extra_hosts: host.docker.internal:host-gateway`）。scheduler 不访问 Ollama，故无 `extra_hosts`。
- **postgres / redis 不发布端口**：compose 里没有 `ports:`，正式部署只开放 Web 入口；日常调试用 `docker compose exec` 进容器操作。
- 数据通过 bind mount 持久化到 `./data/`（不提交 Git）：`data/postgres/`（`PGDATA` 设为 `/var/lib/postgresql/data/pgdata` 子目录）、`data/redis/`（AOF 持久化）、`data/uploads/`、`data/backups/`、`data/logs/`。`docker compose down` 不删 `data/`。

### 1.3 配置与日志约定

配置（`backend/app/core/config.py`，基于 `pydantic-settings` 的 `Settings` 单例）：

- 全部配置从环境变量加载（也支持 `.env` 文件），**密钥一律走环境变量，代码中无硬编码**。
- 模块级 `settings` 单例供全项目 import，迁移与应用永远用同一个数据库。`.env.example` 收录全部配置项。

日志（`backend/app/core/logging.py` 的 `setup_logging(process_name)`）：

- 每个进程（backend / worker / scheduler）独立配置控制台 + 文件双输出，文件写入 `LOG_DIR/<进程名>.log`。
- **日志纪律（第 16 章）**：任何新代码不得记录密码、令牌、API Key、文件原文；数据库连接串含密码，依赖故障只记异常类型名（`type(exc).__name__`），不写连接串。

## 2. 后端结构

代码根：`backend/app/`，入口 `app/main.py`。

### 2.1 core/：横切机制（第 16、17 章）

`backend/app/core/`：

- `config.py` / `logging.py`：见 1.3 节。
- `middleware.py` 的 `RequestContextMiddleware`：每请求生成 UUID `request_id`，写入 `core/request_context.py` 的 contextvars 并回写响应头 `X-Request-ID`。日志、错误响应、审计事件都从 contextvars 自动取用，业务代码无需手传。
- `idempotency.py`：Idempotency-Key 守卫与首次响应落库（见 3.4 节）。
- `errors.py`：`ApiException(code, message, status_code, details)` 是唯一业务异常出口；错误码常量化（`INVALID_CREDENTIALS`、`FORBIDDEN`、`WORK_ITEM_VERSION_CONFLICT`、`IDEMPOTENCY_IN_PROGRESS` 等），新错误码在此登记。
- 统一错误格式（17.1 节）：所有非 2xx 响应为 `{"code", "message", "request_id", "details"}`。`main.py` 注册全局异常处理器：`ApiException` 按自身 code/status 返回；`RequestValidationError` → 422；`HTTPException` → 404/405；未捕获 `Exception` → 500 且只记异常类型名。

### 2.2 api/v1/：路由层与 OpenAPI

`backend/app/api/v1/router.py` 聚合所有领域 router；OpenAPI 自动生成。依赖注入通过 FastAPI `Depends` 串联（`get_current_user` → `get_current_member` / `get_current_leader`、`idempotency_guard`、`get_storage_provider`、`get_model_provider` 等）。新增功能应放进对应领域包，**不要平铺到 `api/` 下**。

### 2.3 domains/：领域服务（4.1 节）

每个领域包结构同构：`models.py` / `service.py` / `schemas.py` / `router.py`，部分含 `state_machine.py`、`dependencies.py`。权限策略集中在 `service.py`（4.1 节"权限策略集中在应用服务层"），`dependencies.py` 提供复用依赖项。

已落地领域：`identity/`（用户/凭据/refresh_tokens，T2.2）；`project/`（单项目配置/`project_members`/`member_capabilities`，T2.3——`members` 是 `project` 的子资源，非独立领域）；`work_items/`（工作项+协作者关联表+8.1 节状态机）；`collaboration/`（协作请求+8.2 节状态机）；`transfers/`（转派申请+8.3 节状态机）；`deadlines/`（DDL 变更申请+8.4 节两级审批）；`notifications/`（站内通知+SSE 流 `stream.py`）；`approvals/`（负责人待审批聚合）；`files/`（`stored_files` 文件元数据与上传/下载）；`deliverables/`（交付物版本化）；`reviews/`（最终审核留痕）；`audit/`（`audit_events` 追加式审计）；`memory/`（四层记忆模块——项目文档索引与检索/成员统计与档案/核心记忆条目与提议确认/历史与经验闭环+知识库问答，设计文档 `docs/2026-08-16-memory-module-design.md`）；`dev_docs/`、`admin/`。

> 注：`agents/` 是顶层包 `app/agents/`，**不在 `domains/` 下**——Agent 编排与业务领域是不同关注点（见第 5 章）。

### 2.4 infrastructure/：基础设施层

- `database/engine.py`：SQLAlchemy 2 异步引擎（`create_async_engine` + asyncpg 驱动），`pool_pre_ping=True` 让连接池取用连接前先探活。`get_session()` 是 FastAPI 依赖注入用的会话工厂。
- `cache/redis.py`：`create_redis_client()` 工厂，基于 `redis.asyncio`，`decode_responses=True`。每次新建客户端而非全局单例，用完 `aclose()`，避免跨事件循环复用连接。
- `queue/queue.py`：Redis List 即时队列（`enqueue`/`dequeue`，LPUSH + BRPOP 构成 FIFO）+ ZSET 延迟队列（`enqueue_delayed`/`promote_due_delayed`，用于 Agent run 指数退避重试）。
- `events/`：SSE 事件通道（与 queue 同级的 Redis 技术机制，生产方横跨四个领域 + worker，故不放领域内）。按成员频道 `agentos:events:{member_id}` 发布，**不在客户端侧过滤他人事件**（16 节最小暴露）。
- `storage/`（第 14 章）：`provider.py` 的 `StorageProvider` 抽象接口（`save/load/delete/exists/iter_chunks` + `stage/commit/discard` 暂存流程）；`local.py` 的 `LocalStorageProvider` 写入配置根目录，`_validate_key` 拒绝绝对路径与 `..`，数据库只保存相对 `storage_key`，不落宿主机绝对路径。`S3StorageProvider` 接口已预留，多后端由 `stored_files.storage_backend` 列承载。`get_storage_provider()` 单例工厂作为 FastAPI 依赖项，测试用 `dependency_overrides` 注入。
- `models/`（第 15 章）：`provider.py` 的 `ModelProvider` ABC（`name`/`model`/`is_external` + `generate(prompt, *, system=None, json_output=False) -> str`）；`ollama.py`（默认）+ `openai_compatible.py`；`errors.py` 的 `ModelError`/`ModelUnavailableError`/`ModelTimeoutError`——httpx 异常一律封装，不外漏。`get_model_provider()` 单例工厂，业务代码不得直接实例化具体模型客户端。**不引入 langchain**——两个 Provider 均用 httpx 直连。
- `models/base.py`：ORM 基类与 Mixin（第 11 章）：`Base`（`DeclarativeBase`）；`UUIDPrimaryKeyMixin`（主键 PostgreSQL UUID，`server_default=gen_random_uuid()`，依赖基线迁移启用的 `pgcrypto` 扩展）；`TimestampMixin`（`created_at`/`updated_at`，`server_default=now()`，`onupdate=now()`）；`VersionMixin`（整型 `version` 字段，用于乐观锁——`work_items`/`collaboration_requests`/`transfer_requests`/`deadline_change_requests`/`deliverables` 继承）；`CoreModel`（`Base + UUID + Timestamp` 抽象基类，需乐观锁再叠加 `VersionMixin`）。

### 2.5 worker/：异步任务消费者（4.2 节）

`backend/app/workers/`：

- `worker.py`：主循环——刷心跳 → `BRPOP` 取任务 → `safe_handle_task` 分发。`safe_handle_task` 包裹单任务异常，确保 Agent run 失败或处理器意外异常不拖垮后续任务消费（第 22 章标准 9）。
- `healthcheck.py` / `heartbeat.py`：Compose healthcheck 执行 `python -m app.workers.healthcheck <name>`，检查 Redis 心跳键（`SET agentos:health:<name> <时间戳> EX 30`）是否存在。进程活着就持续刷新键；进程挂掉 30 秒内键过期，healthcheck 转 unhealthy。
- `due_scan.py`：到期/逾期提醒扫描（T3.6），写通知 + 发 SSE。**Worker 只写通知/事件，不触碰业务状态**（4.2 硬约束）。
- `agent_run.py`：Agent run 执行器（T5.2），从 Redis 队列消费 `agent.run` 任务，注入 checkpointer 执行 LangGraph 图。
- `risk_scan.py`：周期风险扫描任务（T5.4）。

**硬约束**："Worker 不能直接调用'批准、转派、完成'等业务命令，只能生成 Agent 建议或通知。" 后续为 Worker 添加能力时，同样只允许写 `agent_suggestions` / `notifications` 这类"建议与通知"出口，状态变更永远由 API 进程内可鉴权、可审计的 REST 命令完成。

### 2.6 scheduler/：定时扫描（4.2 节）

`backend/app/workers/scheduler.py`：单循环多调度项（各自周期、monotonic 计时）。已注册：`example.ping`（默认 60s）、`due.scan`（默认 300s，到期/逾期提醒）、`agent.risk_scan`（默认 3600s，周期风险扫描，去重：存在 pending/running 的 workflow_risk run 则跳过本轮）。

scheduler 是后续定时任务的**调度挂点**——在循环里按"enqueue 一个任务类型"模式注册新调度项即可，实际执行仍由 Worker 消费。

## 3. 数据层约定

### 3.1 SQLAlchemy Async + Alembic

- 异步引擎 + asyncpg 驱动，`pool_pre_ping=True`（postgres 重启后 `/health` 能自动恢复 200 的原因之一）。
- `backend/migrations/env.py`：连接串**不从 `alembic.ini` 读**，而是 `config.set_main_option("sqlalchemy.url", settings.database_url)`，从统一的 `app.core.config` 拿，保证迁移与应用永远用同一个数据库。online 模式用异步引擎 + `connection.run_sync` 执行迁移；`target_metadata = Base.metadata`，新增模型后 `alembic revision --autogenerate` 即可识别。
- `migrations/versions/0001_baseline.py`：首个基线迁移只做 `CREATE EXTENSION IF NOT EXISTS pgcrypto`。业务表从 0002 起建。
- **启动时自动迁移**：`backend/Dockerfile` CMD 是 `alembic upgrade head && ... && uvicorn ...`，容器每次启动先把库迁到最新，重复执行幂等。
- 迁移序列：`0001_baseline` → `0002_identity_audit_idempotency` → `0003_project_members` → `0004_work_items` → `0005_collaboration_notifications` → `0006_transfers_deadline_changes` → `0007_stored_files` → `0008_deliverables_reviews` → `0009_agent_runs_suggestions` → `0010_agent_runs_prompt`。

### 3.2 audit_events 追加式审计（原则 5、16 节）

- `audit_events` 表：actor_id、action、target_type、target_id、before/after（JSONB 变更摘要）、request_id、source_ip、created_at。**只有 created_at 没有 updated_at**——追加式语义落在模型层面。
- `backend/app/domains/audit/service.py` 的 `record_event(session, actor_id, action, target_type, target_id, before, after)`：request_id 与来源 IP 从 contextvars 自动填充；**只 flush 不 commit**，由业务用例统一 commit，从而与业务写入同生共死（已有测试验证：模拟事件写入失败时业务写入回滚）。
- **全项目不存在任何更新/删除审计事件的代码路径**；查询入口仅 `GET /api/v1/audit-events`（仅项目负责人，普通成员 403、匿名 401）。
- 每次状态迁移/字段变更与审计事件**同一个数据库事务**写入（原则 5）；`assignee_id` 变化必须留痕，为设计文档第 22 章标准 2"历史负责人完整可查"打底。

### 3.3 乐观锁（version 字段）

- `VersionMixin` 提供整型 `version` 字段。更新接口要求客户端携带 `version`；不匹配返回 409（如 `WORK_ITEM_VERSION_CONFLICT`、`COLLABORATION_VERSION_CONFLICT`、`TRANSFER_VERSION_CONFLICT` 等），`details.current_version` 带当前版本号供客户端刷新。
- **关键修复（T6.1）**：transfers / deadlines / work_items 的版本检查原是"应用层读后检查"，交错窗口下两个 approve 都返回 200。修复：`get_request()`/`get_work_item()` 增加 `for_update=True`（`SELECT ... FOR UPDATE`），仅写路径启用（8 个调用点），行锁把并发写串行化，后到者重读新版本后被既有版本检查挡下返回 409。
- 已知同类残留：`collaboration/service.py` 仍是应用层读后检查，存在同型竞态，建议后续版本照 transfers 范本补 `for_update`。

### 3.4 Idempotency-Key 持久化（17.2 节）

- 持久化在 PostgreSQL 表 `idempotency_records`（key、user_id 可空、method、path、首次响应的状态码与 body JSONB、created_at），唯一索引为 `COALESCE(user_id, 零值UUID) + key + method + path`——幂等键与操作者、接口路径绑定，避免跨用户串用。
- `backend/app/core/idempotency.py` 提供两个部件：`idempotency_guard` FastAPI 依赖项（命中已存在记录则抛 `IdempotentReplay`，由异常处理器直接返回首次响应，带响应头 `Idempotency-Replayed: true`，**不重复执行业务写入**）；响应落库中间件（首次成功响应写入记录表）。
- **关键修复（T6.1）**：原"先查后写"在同键并发下各建一条记录，重写为**占位预约模式**——守卫先插入 `response_status=0` 占位记录抢占执行权（唯一索引兜底），其余请求有界等待（10s、50ms 轮询）后重放首次响应；中间件把首次响应写回占位；5xx/异常删除占位允许重试。新增错误码 `IDEMPOTENCY_IN_PROGRESS`。
- 用法：命令类接口声明 `Depends(idempotency_guard)` 即启用。与 `get_current_user` 联用时把后者声明在前，守卫可自动填充 user_id。前端约定：同一操作的自动重试复用同一幂等键（`crypto.randomUUID()` 生成），用户重新点击生成新键。

### 3.5 pgvector 与记忆模块

- postgres 镜像使用 `pgvector/pgvector:pg16`（基于官方 postgres:16，自带 vector 扩展），供记忆模块的语义检索使用。
- `memory_chunks` 列固定为 1024 维（默认 `qwen3-embedding:0.6b`），仅可切换同为 1024 维的模型，切换后须全量重建（`docker compose exec backend python -m app.scripts.rebuild_memory_index --yes`）。**变更维度必须先执行专门的数据库迁移，不能仅运行重建脚本**。首次部署需在宿主机执行 `ollama pull qwen3-embedding:0.6b`。
- 知识库文档**严格不可删除**（admin 也不例外），误传含敏感信息的文件按运维 SOP 在数据库层面对该版本做下架（`UPDATE memory_chunks SET is_current = FALSE WHERE source_type = 'document' AND source_id = '<版本 id>'`），旧块保留供事后审计。**不要直接删除 `stored_files` 行**（会破坏版本链与审计追溯）。
- 文档索引租约（`FILE_INDEX_LEASE_SECONDS` 默认 900 秒 / 恢复扫描 `FILE_INDEX_RECOVERY_INTERVAL_SECONDS` 默认 60 秒）：worker 异常中断后，超过租约的 `indexing` 文件会自动重新投递。

## 4. 认证与权限

### 4.1 密码与令牌（12.1、16 节）

代码位置：`backend/app/domains/identity/`。

- `users` 表：username（唯一）、password_hash、is_active、token_version。`refresh_tokens` 表：user_id、token_hash（**SHA-256**）、expires_at、revoked_at。**库中不存在明文密码与明文 refresh token。**
- 密码用 **Argon2id**（`argon2-cffi`）哈希/校验。Access Token 为 JWT（`PyJWT`，载荷含 `sub`、`tv`=token_version、`type`、`exp`），短期有效（默认 30 分钟）。
- refresh token 为 `secrets.token_urlsafe(48)` 随机串，**只存 SHA-256 哈希**——可撤销（refresh 时旧 refresh token 立即作废，登出即撤销）。禁用用户或提升 `users.token_version` 后旧 Access Token 失效（校验载荷 `tv` 与库中值）。
- `dependencies.py` 的 `get_current_user`：解析 Bearer token、校验 is_active 与 token_version，是后续所有接口的身份入口。

### 4.2 Bootstrap 初始账号

`backend/app/scripts/bootstrap.py`（容器启动命令中 `alembic upgrade head` 之后自动执行，幂等）完成三件事：从 `BOOTSTRAP_ADMIN_USERNAME` / `BOOTSTRAP_ADMIN_PASSWORD`（开发默认 `admin` / `admin123`，生产必须覆盖）创建初始账号；创建默认项目（`BOOTSTRAP_PROJECT_NAME`）；把初始账号登记为该项目的 **leader** 成员。首版不开放公开注册。

### 4.3 项目内权限策略（4.1、6.2 节）

集中在 `backend/app/domains/project/service.py`，`dependencies.py` 提供 `get_current_member` / `get_current_leader`：

- **项目负责人（leader）**：创建成员（同时生成登录账号，**初始密码只在创建响应中返回一次**）、编辑资料、禁用/启用（联动 `users.is_active`，禁用后即无法登录）、确认能力、查询审计事件、最终审核、审批转派/DDL 变更、对 Agent 建议做反馈。
- **成员本人**：填报/修改自己的能力，提交后 `confirmed` 复位为未确认，待负责人确认（6.2 节）。
- **任何项目成员**：`GET /members` 返回全员摘要（含能力与 `active_work_items` 负载统计），**不含密码哈希、令牌等敏感字段**（透明原则 6 与 16 节）。
- **对象级越权返回 403**（`FORBIDDEN`）：如文件下载对无关成员、reviews 反馈对协作者/无关成员、工作项命令对非主执行人。**记录不存在返回 404**（避免泄露存在性）。
- 成员创建/维护/能力确认均同事务写审计事件（`member.created`、`capabilities.submitted`、`capabilities.confirmed` 等）。
- `GET /auth/me` 只返回账号信息（不含角色）。前端拿到 `id`（user_id）后在 `GET /members` 中匹配 `user_id`，得到本人的 member 记录（role、member id），据此控制界面显隐。

## 5. Agent 子系统（第 10、15、17.3 节）

代码位置：`backend/app/agents/`（**顶层包，不在 `domains/` 下**——Agent 编排与业务领域是不同关注点）。子包：`graphs/`、`specialists/`、`prompts/`、`schemas/`、`tools.py`、`service.py`、`router.py`、`models.py`。

核心原则贯穿全章：**人类决定，Agent 建议**（原则 2）——Agent 只生成 `agent_suggestions` 记录，不具备任何写业务状态的工具；人工确认后由前端/用户调用正式业务命令。

### 5.1 ModelProvider 适配层（第 15 章）

`backend/app/infrastructure/models/`：

- `provider.py`：`ModelProvider` ABC（`name`/`model`/`is_external` + `generate(prompt, *, system=None, json_output=False) -> str`）。`get_model_provider()` 单例工厂，`reset_model_provider()` 供测试。验收 grep：`backend/app` 内无对具体 Provider 或 `httpx.AsyncClient` 的直接实例化。
- `ollama.py`（默认，`is_external=False`）：POST `{OLLAMA_BASE_URL}/api/chat`，`format=json` 支持 `json_output=True`；超时/连接失败按 `LLM_MAX_RETRIES` 线性退避重试（瞬时抖动），非 2xx 直接抛错不重试。
- `openai_compatible.py`（`is_external=True`）：POST `{BASE_URL}/chat/completions`，Bearer key，`response_format=json_object`。
- `errors.py`：`ModelError`/`ModelUnavailableError`/`ModelTimeoutError`——httpx 异常一律封装，不外漏（17.3 节上层按统一类型处理）。
- **不引入 langchain**——两个 Provider 均用 httpx 直连（httpx 从 dev 依赖提升为主依赖），满足"不与 Ollama 绑定"且保持依赖最小。
- 配置：`LLM_PROVIDER=ollama`、`LLM_MODEL`、`OLLAMA_BASE_URL=http://host.docker.internal:11434`、`OPENAI_COMPATIBLE_BASE_URL=`、`OPENAI_COMPATIBLE_API_KEY=`、`LLM_TIMEOUT_SECONDS=60`、`LLM_MAX_RETRIES=2`。`settings.llm_is_external` property 供前端提示。

### 5.2 LangGraph 五节点基础图 + PostgreSQL 检查点（10.2 节）

`backend/app/agents/graphs/base.py`：StateGraph 五节点 `load_context → route_capability → run_capability → validate_output → save_suggestion`：

- `AGENT_ROUTES`/`CAPABILITIES` 注册表：agent_type → 能力函数（签名 `(state) -> suggestion dict`，支持 async）。占位 `echo` 能力（不调模型的健康探针，`echo.v1`）用于全链路自检。
- `AgentGraphState` 全 JSON 可序列化。`build_agent_graph(checkpointer)` 由调用方注入检查点；`AsyncPostgresSaver`（langgraph-checkpoint-postgres + psycopg）持久化到 PostgreSQL，DSN 由 `DATABASE_URL` 去 `+asyncpg` 转换，`setup()` 幂等建检查点表，**thread_id = run_id**（重试/恢复天然以 run 为单位，不产生重复建议）。
- **检查点不替代业务记录**（原则 1）：业务状态仍以业务表为准。主库访问仍是 asyncpg，psycopg 仅供检查点，两者并存。
- `agent_runs` 表：status（`pending|running|succeeded|failed`，CHECK）/ agent_type / model / trigger_source（`manual|scheduler|event`）/ work_item_id（FK **可空**，项目级运行为 NULL）/ duration_ms / error / retry_count / request_id / prompt（迁移 0010 追加，人工重试需原样重投）/ 时间戳。
- `agent_suggestions` 表：run_id(FK) / suggestion_type / content JSONB / confidence / risks / fact_refs JSONB / review_status（`pending|accepted|ignored`）/ reviewed_by / reviewed_at / prompt_version / 时间戳。

### 5.3 统一 Pydantic 输出 Schema 与结构校验（10.2、17.3 节）

`backend/app/agents/schemas/suggestion.py`：

- `AgentSuggestionOutput`（模型输出部分）：`suggestion_type`、`content`（`summary`+`rationale` 必填，`extra="allow"` 允许各能力平铺自有字段）、`fact_refs: dict[str, list[str]]`、`confidence ∈ [0,1]`、`risks`、`prompt_version`。`AgentSuggestionEnvelope` 系统侧填充 `run_id`/`model`。
- prompt_version 约定：`<agent_name>.v<N>`，由能力函数随输出声明。
- **结构校验与诊断**：`validate_output` 节点做 Schema 严格校验。失败抛 `SuggestionValidationError`，诊断 JSON（`{"run_id", "stage": "json_parse"|"schema_validate", "errors", "raw_output"截断500字符}`）写入 `agent_runs.error`，run 标记 `failed`，**不落 agent_suggestions、不发通知**。诊断用既有 `failed` 状态，未新增状态、未加迁移。

### 5.4 工具注册表与权限护栏（10.3 节）

`backend/app/agents/tools.py` 的 `TOOL_REGISTRY: dict[str, AgentTool]`，`kind ∈ {"read_query", "write_suggestion"}`：

- **read_query（11 个）**：`get_work_item_overview`、`list_open_work_items`、`list_member_capabilities`、`get_member_workload`、`list_deliverable_metadata`（text/git_link 带 content，file 只给元数据子对象——**不读文件原文**，16 节最小上下文）、`list_blocked_items`、`list_transfer_history`、`list_waiting_collaborations`、`get_work_item_status_counts`、`list_recently_completed_work_items`、`list_pending_approvals`。
- **write_suggestion 唯一写工具**：`write_suggestion()`，只写 `agent_suggestions`。
- **`FORBIDDEN_OPERATIONS`** 结构化列出 10.3 节七项禁止操作（`create_work_item` / `change_assignee` / `approve_transfer` / `change_deadline` / `approve_review` / `delete_file_or_record` / `merge_code`），不注册为工具。
- 模块只 import domain 的 `models`/只读常量，**不 import 任何 domain service**。护栏断言：成功 run 后 `audit_events` 无业务前缀（`work_item.`/`transfer.`/`review.` 等）事件——支撑第 22 章标准 10。

### 5.5 六个辅助 Agent（10.1 节）

`backend/app/agents/specialists/`（每 Agent 一个模块）+ `backend/app/agents/prompts/`（提示词模板，SYSTEM_PROMPT 声明"只输出 JSON"）。公共助手 `specialists/common.py`：`call_model_json()`（经 `get_model_provider()`，json_output=True）与 `build_output()`（解析模型 JSON，**系统侧注入权威字段** suggestion_type/prompt_version/fact_refs——不信任模型自报；非法 JSON 透传给 validate_output 走诊断路径）。

| agent_type | suggestion_type | prompt_version | content 平铺字段 | 触发方式 |
|---|---|---|---|---|
| `requirement_analyst` | `requirement` | `requirement_analyst.v1` | goals/constraints/deliverables/acceptance_criteria | 人工（API） |
| `assignment_advisor` | `assignment` | `assignment_advisor.v1` | recommended_assignee/candidates[]/capability_adjustments[]（仅建议，不自动改能力，6.2 节） | 人工 |
| `planning_advisor` | `planning` | `planning_advisor.v1` | work_item_breakdown[]/collaboration_points[] | 人工 |
| `workflow_risk` | `risk` | `workflow_risk.v1` | risks[{type: overdue|blocked|frequent_transfer|collaboration_wait, severity, detail, ...}] | **scheduler 周期** + 人工 |
| `deliverable_review` | `review` | `deliverable_review.v1` | checklist[{checkpoint, verdict: pass|fail|uncertain, evidence}] | **submit 事件** |
| `summary_agent` | `summary` | `summary_agent.v1` | progress/completed[]/pending_approvals[]/risks[] | 人工（项目级） |

`fact_refs` 由系统侧查询注入（assignment 引用真实 member_ids/work_item_ids；risk 引用 work_item/collaboration/transfer ids；review 引用 deliverable_ids；summary 引用完成/待审统计）。

### 5.6 触发链路与失败恢复

- **人工触发**：`POST /api/v1/work-items/{id}/agent-analysis`（工作项级，权限 leader 或工作项相关成员，复用 `files.service.is_work_item_related`）；`POST /api/v1/agent-analysis`（项目级，仅 leader，work_item_id 可空）。202 返回 `AgentRunOut`；未注册 agent_type → 400。
- **scheduler 周期风险扫描**：`workers/risk_scan.py` + scheduler 任务 `agent.risk_scan`（间隔 `AGENT_RISK_SCAN_INTERVAL_SECONDS` 默认 3600）。去重：存在 pending/running 的 workflow_risk run 则跳过本轮。
- **submit 事件触发初审**：`work_items/service.py` 的 `run_command("submit")` 在业务 commit + 事件发布**之后**调 `_dispatch_deliverable_review()` 投递 `agent.run`（trigger_source="event"）；try/except 尽力而为，投递失败不影响 submit。初审结果只进 agent_suggestions 并通知负责人，**`reviews` 表无 Agent 写入**。
- Summary Agent 仅人工触发（如需周期化照 `agent.risk_scan` 范本挂第四个任务）。
- **唯一触发入口**：`backend/app/agents/service.py` 的 `request_agent_analysis()`——建 run(pending) → Redis 队列投递 `agent.run`。执行器 `app/workers/agent_run.py` 的 `execute_agent_run()`：running → 注入 checkpointer 执行图 → succeeded/failed；成功后发 SSE 事件 `agent.suggestion_ready`。`save_suggestion` 节点：建议与通知同事务，接收人 = 项目 leader。

### 5.7 失败恢复：ZSET 延迟队列指数退避重试 + 人工重新触发（17.3 节）

- `infrastructure/queue/queue.py` 的 ZSET 延迟队列（`enqueue_delayed`/`promote_due_delayed`），与 List 队列同一套机制，无新组件。失败任务 ZADD（score = now + delay），worker 主循环每轮 BRPOP 前把到点任务搬回即时队列。
- 间隔：`delay = AGENT_RUN_RETRY_BASE_SECONDS * 2^attempt`（默认 base=30s，`AGENT_RUN_MAX_RETRIES=3` → 30/60/120，第 4 次失败终态 failed）。
- **两层重试**：provider 层 `LLM_MAX_RETRIES` 是单次调用内线性重试（瞬时抖动）；run 层指数退避是第二道，provider 耗尽后错误才冒泡，不叠加放大。`is_retryable_error()`：模型超时/不可用可重试；`SuggestionValidationError` 是确定性错误不重试。重投沿用原 payload、同一 run_id（thread_id 不变）。
- **人工重新触发**：`POST /api/v1/agent-runs/{id}/retry`：仅 `failed` 可重试（其余 409 `AGENT_RUN_NOT_FAILED`）；权限 leader 或 run 关联工作项相关成员。语义：status→pending、error/duration 清空、**retry_count 清零**，按 run 持久化的原 agent_type/work_item_id/prompt 重投，202 返回 `AgentRunOut`。
- **核心流程不受影响**（第 22 章标准 9）：`safe_handle_task` 包裹单任务异常，Agent run 失败不拖垮后续任务消费。宿主机无 Ollama 时全部 Agent run 干净地落 failed，核心流程不受影响。

### 5.8 建议查询与反馈闭环（12.5、13.1 节）

`backend/app/agents/router.py`：

| 端点 | 说明 | 权限 |
|---|---|---|
| `GET /agent-suggestions` | 过滤：suggestion_type/review_status/work_item_id + limit/offset | 登录成员可读 |
| `POST /agent-suggestions/{id}/feedback` | `{action: "accepted"|"ignored"}`；重复反馈 409 `AGENT_SUGGESTION_ALREADY_REVIEWED` | 仅 leader |
| `GET /agent-runs` / `GET /agent-runs/{id}` | 运行记录列表/详情（error/duration_ms/retry_count），前端引导流程轮询用 | 登录成员可读 |
| `GET /config` | `{llm_provider, llm_is_external}`，只暴露非敏感标识 | 登录成员 |

反馈写审计 `agent.suggestion_feedback`（`agent.` 前缀，不触碰护栏断言的业务前缀清单）。建议查询全员可读、反馈仅 leader——建议本身无敏感信息，与团队透明语义一致。

## 6. 前端结构（第 13 章）

### 6.1 技术栈与组件体系

- Vite 6 + React 18 + TypeScript 5、react-router-dom 6、TanStack Query 5、zustand 5。
- **shadcn/ui 硬性约定**：本项目前端使用 shadcn/ui，严格使用对应组件，绝不越界。antd 已彻底移除（依赖与 import 零残留）。一切 UI 元素必须使用 `frontend/src/components/ui/` 下由 `npx shadcn@latest add` 生成并入库的官方组件。shadcn 有对应组件就不准手写替代品；表单一律用 shadcn Form（react-hook-form + zod）；反馈一律用 Sonner；布局用 Tailwind 工具类；图标用 `lucide-react`。
- 技术底座：Tailwind v4 + `@tailwindcss/vite` 插件（`vite.config.ts`），`@` 路径别名，主题变量在 `src/index.css`。

### 6.2 app/：应用骨架

- `src/main.tsx` → `src/app/App.tsx`：组装 `QueryClientProvider`、`RouterProvider`。`src/app/router.tsx`：`/login` 独立，其余页面挂在 `AppLayout` 下。`components/RequireAuth.tsx` 路由守卫。
- `src/app/store.ts`：zustand + persist（localStorage）存 access/refresh token、当前 user 与 member；导出 `useIsLeader`。
- `frontend/src/services/api.ts`：统一 API 客户端——所有请求自动拼 `/api/v1` 前缀；自动从 `useAuthStore` 取 token 加 `Authorization: Bearer` 头；响应非 2xx 时若 body 符合统一错误格式则抛出 `ApiError`（含 `code`/`message`/`requestId`/`details`/`status`），否则包装成 `HTTP_<status>`；401 时单例 refresh 并重试一次（失败清登录态跳 /login）；`newIdempotencyKey()` 用 `crypto.randomUUID()` 生成幂等键；409 透出 `ApiError.isVersionConflict`；204 返回 `undefined`。`api.upload` 用 XHR + FormData（XHR onprogress 进度条），`api.downloadFile` 从 Content-Disposition 解 RFC 5987 文件名。
- `frontend/src/services/events.ts`：`useEventStream()` 在 AppLayout 挂载；accessToken 变化触发重连或关闭。对命名事件按 type 前缀失效 TanStack Query 缓存（`work_item.*`→work-items；`collaboration.*`→collaboration-requests；`transfer.*`→approvals+work-items；`deadline_change.*`→approvals+work-items+collaboration-requests；`reminder.*`→work-items+collaboration-requests 并 toast.warning；`review.*`→work-items+reviews+approvals；`agent.suggestion_ready`→agent-suggestions+agent-runs；任何事件失效 notifications 与 audit-events）。断线靠 EventSource 自动重连，漏发由"收任意事件即失效相关缓存"兜底。
- SSE 端点 `GET /api/v1/events/stream`（`backend/app/domains/notifications/stream.py`）：EventSource 无法自定义请求头，用 `?token=<access_token>`；连接即送 `: connected`，15s 无事件发 `: ping` 心跳帧防代理超时；`Last-Event-ID` 仅接受不补发。SSE 只读，所有写操作走 REST；接口清单中无 WebSocket（4.3 节）。nginx 有专用 location（`proxy_buffering off`、1 小时读超时）。
- **publish 时机必须在 DB commit 之后**：业务 service 在 notify 处传 `outbox=events` 收集 `OutgoingEvent`，commit 成功后 `publish_after_commit(events)`（自建短连接，失败仅 warning——通知表是兜底通道）。幂等重放不执行 service，天然不重复发布。

### 6.3 features/：按业务功能组织

`frontend/src/features/`：

- `auth/`：登录页（`LoginPage.tsx`，shadcn Form）；`session.ts` 的 `loadIdentity()` 调 `/auth/me` + `/members` 按 user_id 匹配本人成员记录。
- `members/`：成员 Table（角色/能力 Badge 含待确认态/活跃任务数/可投入时间）；负责人可创建成员（成功后 Dialog 展示一次性 initial_password）、编辑、禁用/启用、确认能力；成员本人"填报我的能力"。
- `work-items/`：列表页（过滤：负责人/状态/DDL 区间）、详情页含命令按钮（显隐规则：发布=负责人+DRAFT，开始/阻塞/解除阻塞/提交=主执行人+对应状态，取消=负责人+未终态），均携带当前 version 与幂等键；创建/编辑共用 `work-item-form.tsx` Dialog 表单；"AI 需求引导"入口。
- `collaboration/`：工作项详情页挂三个区——`CollaborationSection`（协作列表 + 发起协作 Dialog，按当前用户身份与状态渲染操作按钮）、`TransferSection`（申请转派 Dialog + 历史）、`DeadlineChangeSection`（DDL 变更申请 Dialog + 历史）。
- `deliverables/`：`DeliverableSection.tsx`（版本历史 + "提交交付" Dialog 三类型切换 + 审核反馈区，403 时静默不渲染）、`FileUploadField.tsx`（XHR onprogress 进度条，前置校验 20MB/扩展名白名单）、`DeliverableBody.tsx`（三类内容渲染，详情页与审批 Dialog 共用）。
- `approvals/`：Tabs「待我审批」（仅负责人，`GET /approvals` 卡片 + 交付审核 `DeliveryReviewSection` 即 `GET /work-items?status=IN_REVIEW`）/「我的申请」。
- `dashboard/`：`DashboardPage.tsx`（团队透明看板雏形——状态分布卡片、全员工作量表、7 天内到期列表，由 `GET /members` + `GET /work-items` 前端聚合，`GET /dashboard` 接口未实现）；`TodoSection`（待处理中心，登录首页，五类聚合）；`TimelineSection`（项目时间线，仅负责人，`GET /audit-events` 事件流，31 个 action 中文映射）。
- `notifications/`：`NotificationBell`（顶栏未读数 Badge + DropdownMenu 最近 20 条）。
- `agent-assistant/`：`AgentAssistantPage.tsx`（建议中心——类型/状态过滤、建议卡片、采纳/忽略按钮仅 leader 且 pending、运行记录表 failed 行"重新触发"）；`SuggestionContent.tsx`（六类 content 结构化渲染）；`RequirementGuidedCreateDialog.tsx`（自然语言输入 → `POST /agent-analysis` → 轮询 `GET /agent-runs/{id}` → 预填**可编辑**表单 → 确认后调既有 `POST /work-items` 并 best-effort 写 accepted 反馈；**忽略则只写 ignored 反馈，无任何业务写入**——原则 2）。外部数据提示：`GET /config` 的 `llm_is_external=true` 时显示琥珀色警示。
- `admin/`：全局管理控制台。

通知事件接收人规则：协作类通知发给对端（`collaboration.requested`→接收人、`accepted/declined/submitted`→发起人、`revision_requested/completed`→接收人）；`transfer.requested`/`deadline_change.requested`→全体活跃负责人；`transfer.approved`→发起人+新负责人；各类 `rejected`→申请人；`deadline_change.approved`→协作级自动生效则协作对端、负责人批准则申请人；`reminder.due_soon/overdue`→主执行人/协作接收人；`agent.suggestion_ready`→项目 leader；`review.approved|changes_requested|rejected`→主执行人（通知正文只含结论摘要，**不含反馈正文**）。`work_item.*` 状态变化只发 SSE 不写通知行；`deliverable.*`/`file.*` 只是审计 action 不发 SSE。通知 body 仅摘要，审批意见只进审计不进通知（16 节）。

### 6.4 状态管理

- TanStack Query 5：服务端数据请求/缓存（与 SSE 缓存失效策略配合）。
- zustand 5：轻量全局状态（token、user、member），persist 到 localStorage。

## 7. 测试体系（第 18 章）

### 7.1 后端五维

全部测试在容器内运行：`docker compose exec backend pytest` 或 `docker compose run --rm --no-deps -v "$PWD/backend:/app" backend python -m pytest tests/ -q`（`-v` 挂载宿主机源码保证跑的是实时代码，镜像内是构建时快照）。`tests/conftest.py` 在任何 app 导入前把 `DATABASE_URL` 库名改写为 `<原名>_test`、Redis 切到 db 15；会话级自动建库 + `alembic upgrade head`；每用例后 TRUNCATE 全部业务表（含 LangGraph 检查点表，存在才清）；每用例结束 dispose 引擎，规避 pytest-asyncio 跨事件循环连接复用问题；**不污染 `agentos` 主库**。提供 `project`/`leader` fixture 与 `add_member`/`auth_headers` 辅助函数。

**并行跑多套测试**：给 `DATABASE_URL` 指定不同库名、给 `REDIS_URL` 指定非 0 的 db（如 13/14）即可互相隔离——`conftest.py` 只在 Redis 路径为 `/0` 或空时才强制改写为 `/15`。

| 维度 | 文件 | 覆盖点 |
| --- | --- | --- |
| 领域单元 | `test_unit_permissions.py`、`test_unit_transfer_rules.py`、`test_unit_deadline_rules.py` | 权限策略缺口（非主执行人/无关第三方/禁用成员分支）；7.3 节转派规则；7.4 节 DDL 影响规则 |
| API 集成 | `test_api_gaps_t6b.py` | 对照第 12 章逐端点补缺：404/422/409 错误分支、六命令端点对不存在资源、BLOCKED 取消 409、三类审批的版本冲突 |
| 并发 | `test_concurrency.py` | `asyncio.gather` 真并发：重复审批确定性 [200,409]、乐观锁同版本 PATCH 一胜一负、同幂等键并发创建只建一条 |
| Agent 合约 | `test_agent_contract.py` | `StubModelProvider` 替身（不依赖 Ollama）：合法输出成功且系统侧权威字段覆盖模型自报、Schema 非法/非 JSON/JSON 数组走诊断落 failed、超时不可用落 failed；**所有失败路径零建议、零通知、零新增业务审计**（17.3 节） |
| 审计 | `test_audit_coverage.py` | 9 类关键动作逐一断言审计动作/actor/target/before-after；审计不可变（写方法 405/404）；已实证"删除任一审计写入，对应测试必失败" |

辅助模块（tests 包内，可 import）：`helpers_t6a.py`（场景构造 + storage fixture）、`helpers_t6b.py`（agent run 驱动）、`helpers_e2e.py`（e2e 场景共用：替身 Provider、业务状态快照 `snapshot_business_state`、审计回放断言 `assert_audit_replay`）。新增测试优先复用这些 helper，不要再改 `conftest.py`。

**实现陷阱备忘（后续阶段复用）**：双外键指向同一张表时 SQLAlchemy relationship 需 `foreign_keys` 消歧；`updated_at` 因 `onupdate=func.now()` 在 UPDATE 后属性过期，commit 后需 `session.refresh()` 再序列化；唯一约束下"替换集合"（如能力列表、协作者列表）必须先清空 flush 再写入；SSE 端到端测试用裸 ASGI 调用真实 app（httpx 0.28 的 ASGITransport 不支持流式）；**凡涉及并发/时序断言，先证明它能稳定失败再相信它能稳定通过**。

### 7.2 前端 Vitest（18.2 节）

- devDependencies：`vitest`、`@testing-library/react` / `user-event` / `jest-dom` / `dom`、`jsdom`（未升级任何既有依赖）。
- `frontend/vitest.config.ts`（jsdom 环境、`@` alias）；`src/test/setup.ts`（jest-dom + Radix polyfill + 每用例清理登录态）；`src/test/mock-api.ts`（`vi.mock` 替换 `services/api` 的 `api` 对象，保留真实 `ApiError`）；`src/test/fixtures.ts`（夹具工厂）；`src/test/render.tsx`（`renderWithProviders` / `signInAs`）。
- 脚本：`npm run test`（`vitest run`）、`npm run test:watch`。覆盖：组件测试（含幂等键断言与 409 冲突提示、状态徽标七状态四优先级全映射）；页面集成（`leader-flow` 与 `member-flow`，权限差异有断言）；e2e 骨架（`src/__tests__/e2e/full-workflow.test.tsx`，5 可跑 + 5 `it.todo`）。前端无独立 build 测试，`npm run build`（tsc strict + noUnusedLocals）是唯一静态检查。

### 7.3 RAG e2e 串行 + 双任务并行（第 9 章、18.3 节）

两个场景都是 pytest 文件、单测试函数内跑完整时序（conftest 每用例后清库），与后端测试同一键入口。

- `tests/test_e2e_rag_serial.py`（T6.3）：按第 9 章时序——负责人分配 RAG 工作项 → 申请转派 → 负责人审批（主责任转移）→ 两次协作（含文件回传）→ 提交 Git 链接+评估文本+说明文件 → 审核通过 COMPLETED → 归档证据文件。逐步断言审计与通知；协作无需审批、转派必须审批；场景中段用替身 Provider 驱动 Agent run，**运行前后业务状态快照完全一致**。末尾 `assert_audit_replay` 按审计事件重建完整时序与 25 步预期逐一比对。
- `tests/test_e2e_parallel.py`（T6.4）：RAG 任务与 Agent 工具设计任务由不同成员**并行**推进（`asyncio.gather` 交错每一步）。断言：两个任务审计目标集合不相交、通知互不串扰、负载推进中=1/完成后=0、Agent 建议不改业务状态、各自审计链可独立回放。

## 8. 部署与运维（19.4 节）

### 8.1 Docker Compose 六服务编排

- `docker-compose.yml` 编排六服务（见 1.2 节）。数据通过 bind mount 持久化到 `./data/`（不提交 Git）：`data/postgres/`（`PGDATA` 设为 `/var/lib/postgresql/data/pgdata` 子目录）、`data/redis/`（AOF 持久化）、`data/uploads/`、`data/backups/`、`data/logs/`（backend / worker / scheduler 三个进程各自的 `<进程名>.log`）。`docker compose down` 不删 `data/`，数据安全；要重置环境需手动清空 `data/postgres/` 等目录（谨慎操作）。

### 8.2 本地文件存储（第 14 章）

- 配置项进 `core/config.py`（compose 与 `.env.example` 已接）：`STORAGE_BACKEND=local`、`STORAGE_ROOT=/app/data/uploads`、`UPLOAD_MAX_BYTES=20971520`（20MB）、`UPLOAD_ALLOWED_EXTENSIONS`（.txt,.md,.csv,.json,.pdf,.png,.jpg,.jpeg,.zip）、`UPLOAD_ALLOWED_MIME_TYPES`（对应逗号列表，config 以 property 解析为集合）。
- **禁止直接暴露上传目录**：`frontend/nginx.conf` 无 `data/uploads` 映射，API 是唯一入口。业务层只依赖 `StorageProvider` 接口——grep 验证 `app/domains/`、`app/api/` 无文件系统路径 / `LocalStorageProvider` / `os.replace` 引用。
- 暂存区与正式目录同文件系统，保证 `os.replace` 原子落位；按哈希前两位分桶 `63/63ed…_<rand>`。**补偿清理**（17.2 节）：落库失败时删除已落盘文件，磁盘无残留。上传大小校验在流式暂存期间进行，超限即断流删暂存；客户端前置校验只是体验优化，后端白名单为准。

### 8.3 备份恢复脚本（19.4 节）

- `deploy/scripts/backup.sh`：pg_dump 自定义格式 → `data/backups/postgres/`；`tar --listed-incremental` 增量 → `data/backups/uploads/`；14 天保留自动清理；日志写 `data/logs/backup.log`。
- `deploy/scripts/restore.sh`：恢复到**指定目标库**（覆盖主库必须 `--confirm`，已实测拒绝）；恢复后自动校验库可连、核心表存在、`stored_files` 随机抽查 SHA-256 与实际文件比对。定时触发靠宿主机 crontab（`deploy/scripts/README.md` 有配置方法）。恢复演练记录见 `docs/quality-baseline-2026-07-29.md` 第 3 节。
- 遗留说明：增量包只含当次变更，精确恢复到某天需按序解包"全量基线 + 增量包"。

### 8.4 健康检查与编排联动

- `GET /health`（`app/main.py`）检查三项：进程本身、`SELECT 1` 探 PostgreSQL、`PING` 探 Redis，返回 `{"status": "ok|degraded", "checks": {...}}`，全部 ok 才返回 200，否则 503。依赖故障只记异常类型名，不把连接串写进日志。`frontend/nginx.conf` 的 `location = /health` 直通 backend 的 `/health`，方便从 Web 端口探活。
- postgres / redis healthcheck 分别为 `pg_isready` 和 `redis-cli ping`，`retries: 12`。backend healthcheck 用 Python `urllib` 请求 `/health`（镜像里没有 curl/wget），`start_period: 20s` 覆盖 `alembic upgrade head` 的启动耗时。worker / scheduler healthcheck 是 `python -m app.workers.healthcheck <worker|scheduler>`：读取 Redis 心跳键判断进程存活——进程挂掉 30 秒内键过期，healthcheck 转 unhealthy。

### 8.5 镜像源与构建

- 本机 Docker Hub 不可达时：基础镜像从 `docker.m.daocloud.io` 拉取后 `docker tag` 回官方名。pip / npm 源通过 compose 的 build args 切换（`PIP_INDEX_URL` / `NPM_REGISTRY`，Dockerfile 内默认官方源，compose 覆盖国内镜像）。**buildx 版本要求 ≥ 0.17**。
- **源码打进镜像而非挂载**：改了后端代码必须重建镜像再跑测试（`docker compose build backend worker scheduler && docker compose up -d`）。前端 `frontend/Dockerfile` 两阶段构建：`node:20-alpine` 构建产物 `dist/` → `nginx:1.27-alpine` 拷贝 `dist/` 和 `nginx.conf`。
- `frontend/nginx.conf` 规则：`location /` SPA 回退；`location /api/` 反代到 `http://backend:8000`（携带真实客户端 IP 头）；`location = /health` 直通；`location /api/v1/events/stream` 专用（`proxy_buffering off`、1 小时读超时）。

### 8.6 安全检查清单与发布文档（第 16、22 章）

- 安全检查（`docs/quality-baseline-2026-07-29.md` 第 4 节）：凭据、权限、日志、模型最小上下文、网络面五项检查，**全部通过，无需代码修复**；唯一提醒：宿主机系统级 postgres 监听 0.0.0.0:5432，建议收紧。
- `backend/scripts/benchmark.py`（性能基线见 `docs/quality-baseline-2026-07-29.md` 第 2 节）：101 工作项量级下读接口 p95 < 35ms、命令 p95 < 70ms、登录约 96ms、SSE 建立 < 16ms；未发现慢查询，未新增索引。增长后按基线报告复测，首要关注负载聚合与审计表扫描。
- `docs/release-guide.md`：Debian 宿主机准备、开发模式、`.env.example` 逐项说明、备份恢复入口、部署验证记录（标准 11）。
- MVP 核对（`docs/quality-baseline-2026-07-29.md` 第 1 节）：第 22 章 13 条标准逐条核对 + 证据指针，**全部满足，MVP 宣告完成**。

---

> 关键设计原则散见各章，最核心约定：业务状态以 PostgreSQL 为唯一权威（原则 1）；Agent 不直接修改正式业务状态（原则 2 / 4.2 节）；audit_events 追加式不可覆盖、与业务写入同事务（原则 5）；对象级越权返回 403、记录不存在返回 404；统一错误格式 + X-Request-ID 由 contextvars 自动取用；并发表用 version 乐观锁 + 写路径 SELECT ... FOR UPDATE 行锁；Idempotency-Key 占位预约模式；shadcn/ui 硬性约定（antd 已移除）；日志不得记录密码/令牌/API Key/文件原文。
