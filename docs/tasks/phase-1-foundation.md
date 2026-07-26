# 阶段 1：工程基础

- 对应设计文档：第 20 章"阶段 1：工程基础"，另涉及第 4、5、19 章。
- 阶段目标：搭建可在 Debian 上通过 Docker Compose 一键启动的工程基线，包括 FastAPI 后端、React 前端、PostgreSQL、Redis、Worker、Scheduler 六个服务，以及迁移、配置、日志和健康检查。

## T1.1 Docker Compose 六服务基线与 data/ 持久化

- 状态：待开始

**目标**

按设计文档 19.2 节建立 `frontend` / `backend` / `worker` / `scheduler` / `postgres` / `redis` 六个服务的 Compose 编排，并按 19.4 节完成 `data/` 目录持久化。

**具体内容**

- 在 `docker-compose.yml` 中定义六个服务；Ollama 不放入 Compose，后端与 Worker 通过 Linux `host-gateway` 访问宿主机 `11434` 端口（19.2 节）。
- PostgreSQL 与 Redis 使用官方镜像并挂载数据卷；数据库、Redis 不对公网开放（19.4 节）。
- 按第 5 章仓库结构建立 `data/postgres/`、`data/redis/`、`data/uploads/`、`data/backups/`、`data/logs/` 目录并配置挂载；`data/` 不提交 Git。
- 提供 `.env.example`，包含数据库连接、Redis 连接、LLM 配置项（`LLM_PROVIDER`、`LLM_MODEL`、`OLLAMA_BASE_URL` 等，第 15 章）。
- `backend/Dockerfile` 与 `frontend/Dockerfile` 提供可构建的基础镜像。

**依赖**

无

**验收标准**

- `docker compose up -d` 在 Debian 宿主机上能拉起全部六个服务且无启动报错。
- 删除并重建容器后，`data/postgres/` 中的数据不丢失。
- `.env.example` 覆盖全部必需配置项，仓库中不包含真实密钥。

## T1.2 后端 FastAPI 骨架、配置与日志

- 状态：待开始

**目标**

按 4.1 节建立模块化单体的 FastAPI 工程骨架，完成统一配置管理与日志规范。

**具体内容**

- 按第 5 章建立 `backend/app/` 结构：`api/v1/`、`core/`、`domains/`（identity、project、work_items、collaboration、transfers、deadlines、deliverables、reviews、audit、notifications 十个领域包占位）、`infrastructure/`（database、cache、queue、storage、models、integrations）、`workers/`。
- `core/` 中实现基于环境变量的配置加载，禁止在代码中硬编码密钥。
- 日志统一输出到 `data/logs/`，并遵守第 16 章规定：日志不记录密码、令牌、API Key 和文件原文。
- `backend/pyproject.toml` 固定 FastAPI、SQLAlchemy 2 异步、Alembic 等依赖版本。

**依赖**

T1.1

**验收标准**

- 后端容器启动后能通过 `GET /api/v1` 下的一个占位路由返回 200。
- 日志文件写入 `data/logs/`，且一次模拟登录请求的日志中不出现密码或令牌原文。
- 领域目录结构与第 5 章清单一致。

## T1.3 PostgreSQL 接入与 Alembic 迁移基线

- 状态：待开始

**目标**

按第 11 章建立 SQLAlchemy 2 异步数据访问层，并用 Alembic 建立可重复执行的迁移基线。

**具体内容**

- `infrastructure/database/` 中实现异步引擎与会话管理，连接配置来自 T1.2 的配置模块。
- `backend/migrations/` 初始化 Alembic，生成首个空基线迁移（或仅含扩展，如 `pgcrypto`）。
- 约定所有核心表包含 `created_at`、`updated_at`，需要并发保护的表包含 `version`，主键使用 PostgreSQL UUID（第 11 章），以基类或 Mixin 形式沉淀。
- Compose 中 backend 启动前自动执行 `alembic upgrade head`。

**依赖**

T1.2

**验收标准**

- 从零初始化数据库执行 `alembic upgrade head` 成功，重复执行幂等。
- 基类/Mixin 提供 UUID 主键与 `created_at`/`updated_at` 字段，可被后续领域模型直接继承。

## T1.4 健康检查接口与 Compose 编排联动

- 状态：待开始

**目标**

为 backend、worker、scheduler 提供健康检查，并让 Compose 依赖关系基于健康状态排序启动。

**具体内容**

- backend 提供健康检查接口（如 `GET /health`），检查应用进程、PostgreSQL 连接、Redis 连接三项。
- Compose 中为 postgres、redis 配置 `healthcheck`，backend/worker/scheduler 通过 `depends_on: condition: service_healthy` 等待依赖就绪。
- worker、scheduler 进程提供可被 Compose 探测的存活性信号（如心跳写 Redis 或进程级检查命令）。

**依赖**

T1.2、T1.3

**验收标准**

- 数据库未就绪时 backend 不提前启动；全部服务 `docker compose ps` 显示 healthy。
- 手动停掉 postgres 容器后，backend 健康检查接口返回非 200，恢复后自动转回正常。

## T1.5 前端 React 骨架与 API 客户端

- 状态：待开始

**目标**

按第 13 章建立 React + TypeScript + Vite 前端骨架，接入路由、数据请求与组件库。

**具体内容**

- 按第 5 章建立 `frontend/src/` 结构：`app/`、`features/`（auth、dashboard、members、work-items、approvals、deliverables、agent-assistant 占位）、`components/`、`services/`、`types/`。
- 接入 React Router、TanStack Query、轻量全局状态库和管理后台组件库（第 13 章）。
- `services/` 中实现统一 API 客户端：自动拼接 `/api/v1` 前缀、携带认证头、处理第 17.1 节统一错误格式（先按接口约定实现，后端在 T2.1 落地）。
- 配置前端容器在 Compose 中通过反向代理或开发服务器对外提供 Web 入口（19.4 节：正式内部部署只开放 Web 入口）。

**依赖**

T1.1

**验收标准**

- 前端容器启动后可访问首页占位路由，各 features 目录有对应空页面。
- API 客户端能在响应为统一错误格式时抛出结构化错误（含 `code`、`message`、`request_id`）。

## T1.6 Worker 与 Scheduler 进程骨架

- 状态：待开始

**目标**

按 4.2 节建立与 API 共用领域层的后台 Worker 与 Scheduler 进程，打通 Redis 队列。

**具体内容**

- `app/workers/` 中实现 Worker 入口：从 Redis 队列消费任务，复用 `domains/` 与 `infrastructure/` 同一套领域模型和数据库访问层（4.2 节）。
- `infrastructure/queue/` 封装 Redis 任务入队/出队接口，供 API 进程投递后台任务（如 Agent 运行、到期提醒）。
- Scheduler 进程实现定时触发入口，为阶段 3/5 的到期提醒、逾期风险扫描、日报等调度预留挂点（4.2 节），首版只需能注册并触发一个示例任务。
- 在 Worker 骨架中落实约束：不调用"批准、转派、完成"等业务命令的代码路径，仅提供生成建议或通知的能力（4.2 节）。

**依赖**

T1.2、T1.3

**验收标准**

- API 进程投递一个示例任务后，Worker 容器日志显示成功消费。
- Scheduler 按配置周期触发示例任务并写入日志。
- Worker 进程代码中不存在对审批/转派/完成类业务命令的调用（可通过代码审查确认）。
