# 阶段 1 开发者指南：工程基础

本文面向刚加入 AgentOS 的开发者，说明阶段 1（T1.1–T1.6）实现的工程基线**实际长什么样、为什么这么做**。文中章节号（如 4.1、11、19.2）均指设计文档 `docs/2026-07-26-agentos-workflow-platform-design.md` 的章节。阶段 1 只做骨架，不实现业务逻辑——看到 501、占位页面、空包都是预期状态。

## 1. 整体架构（对应第 4 章）

仓库通过 Docker Compose 编排六个服务，对应 19.2 节：

| 服务 | 角色 | 镜像 | 进程 |
|---|---|---|---|
| `frontend` | Web 入口，nginx 提供静态资源并反代 `/api/` | `agentos-frontend` | nginx |
| `backend` | FastAPI 模块化单体（4.1），唯一对外 API | `agentos-backend` | `alembic upgrade head && uvicorn` |
| `worker` | 后台任务消费者（4.2），与 API 共用代码 | `agentos-worker` | `python -m app.workers.worker` |
| `scheduler` | 周期任务触发器（4.2） | `agentos-scheduler` | `python -m app.workers.scheduler` |
| `postgres` | 主数据库（第 11 章） | `pgvector/pgvector:pg16`（基于 postgres:16，含 vector 扩展，供记忆模块使用） | postgres |
| `redis` | 任务队列 + 心跳媒介（4.2） | `redis:7-alpine` | redis-server（AOF 开启） |

要点：

- **进程边界**：backend / worker / scheduler 是三个独立容器，但构建自同一个 `backend/` 镜像（`backend/Dockerfile`），只是启动命令不同。这正是 4.2 节"Worker 与 API 使用同一套领域模型和数据库访问层，但运行在独立进程中"的落地方式。
- **启动顺序基于健康检查**：`postgres`、`redis` 自带 healthcheck；`backend`/`worker`/`scheduler` 通过 `depends_on: condition: service_healthy` 等待两者就绪后才启动（见 19.2 与 T1.4）。`frontend` 只等 backend `service_started`（它只是静态资源 + 反代，不需要等 backend healthy）。
- **Ollama 不在 Compose 内**（19.2）：LLM 运行在宿主机，backend/worker 通过 `host.docker.internal` 访问宿主机的 `11434` 端口。

## 2. docker-compose.yml 逐服务说明

### postgres / redis

- 数据库使用 `pgvector/pgvector:pg16` 镜像（基于官方 postgres:16，自带 vector 扩展，记忆模块的语义检索依赖它），redis 使用官方镜像 `redis:7-alpine`，数据通过 bind mount 持久化到 `./data/postgres`、`./data/redis`（19.4）。
- redis 以 `--appendonly yes` 启动（AOF 持久化），防止队列数据因重启丢失。
- **两者都不发布端口**（compose 里没有 `ports:`）。原因有二：一是 19.4 节要求数据库和 Redis 不对外开放，正式部署只开放 Web 入口；二是本机宿主机 5432/6379 已被系统服务占用。日常调试用 `docker compose exec` 进容器操作（见第 8 节）。`.env.example` 注释中"端口仅绑定 127.0.0.1"描述的是同一安全目标的通用形态，本仓库实现得更严格——完全不绑定。
- `PGDATA` 设为 `/var/lib/postgresql/data/pgdata` 子目录，避免 bind mount 根目录的权限/残留文件问题。
- healthcheck 分别为 `pg_isready` 和 `redis-cli ping`，`retries: 12` 给足冷启动时间。

### backend

- `build.context: ./backend`，构建参数 `PIP_INDEX_URL` 控制 pip 源（见第 8 节镜像源说明）。
- 端口 `8000:8000` 对外发布，是 API 直达入口（前端反代之外，调试可直接访问）。
- 环境变量全部走 `${VAR:-default}` 形式，默认值即可跑通开发环境；`DATABASE_URL` 指向 Compose 内部服务名 `postgres`。
- `extra_hosts: host.docker.internal:host-gateway`：Linux 下 Docker 默认没有 `host.docker.internal` 这个 DNS 名，`host-gateway` 把它解析为宿主机在 docker0 网桥上的地址，从而访问宿主机上的 Ollama（`OLLAMA_BASE_URL=http://host.docker.internal:11434`，19.2 节）。
- healthcheck 用 Python `urllib` 请求 `/health`（镜像里没有 curl/wget），`start_period: 20s` 覆盖 `alembic upgrade head` 的启动耗时。
- 挂载 `./data/logs`、`./data/uploads`，日志与上传文件落在宿主机 `data/` 下。

### worker / scheduler

- 同一镜像，仅覆盖 `command`。不发布任何端口。
- healthcheck 是 `python -m app.workers.healthcheck <worker|scheduler>`：读取 Redis 心跳键判断进程存活（机制见第 5 节）。
- scheduler 额外注入 `SCHEDULER_EXAMPLE_INTERVAL_SECONDS`（默认 60）控制示例任务周期。
- scheduler 不需要访问 Ollama，所以没有 `extra_hosts`（保持最小配置）。

### frontend

- 端口 `3000:3000` 对外发布，是整个系统对用户的唯一 Web 入口（19.4）。
- healthcheck 用 `wget`（nginx alpine 镜像自带）探测本机 3000 端口。

## 3. backend 逐模块说明

代码根：`backend/app/`。入口 `app/main.py`。

### core/：配置与日志（T1.2，第 16 章）

- `core/config.py`：基于 `pydantic-settings` 的 `Settings` 类，全部配置从环境变量加载（也支持 `.env` 文件），包括 `DATABASE_URL`、`REDIS_URL`、LLM 配置（第 15 章）、日志目录、scheduler 周期。**密钥一律走环境变量，代码中无硬编码**；模块级单例 `settings` 供全项目 import。
- `core/logging.py`：`setup_logging(process_name)` 为每个进程（backend / worker / scheduler）配置控制台 + 文件双输出，文件写入 `LOG_DIR/<进程名>.log`。第 16 章规定日志不记录密码、令牌、API Key 和文件原文——这一约束靠**调用方纪律**落实：占位登录路由 `POST /api/v1/auth/login` 只记录 `username`，从不记录 payload 里的密码字段（`api/v1/router.py`）。验收时用含密码的登录请求验证过 `data/logs/` 中无密码原文。

### api/v1/：占位路由（T1.2）

`api/v1/router.py` 目前三个路由：

- `GET /api/v1/`：返回 `{"service","api","status"}` 的存活占位。
- `POST /api/v1/auth/login`：返回 **501** + 统一错误格式（17.1 节），真实认证在 T2.1 落地。
- `POST /api/v1/tasks/example`：向 Redis 队列投递 `example.ping` 任务，用于验证 API → 队列 → Worker 链路（T1.6 验收用）。

### domains/：十个领域占位包（4.1）

`identity`、`project`、`work_items`、`collaboration`、`transfers`、`deadlines`、`deliverables`、`reviews`、`audit`、`notifications` 十个空包，与 4.1 节一一对应。注意 4.1 节还提到 `agents` 领域，本实现把它单列为顶层包 `app/agents/`（含 `graphs/specialists/prompts/schemas` 子包，对应第 10 章 LangGraph 流程），因为 Agent 编排与业务领域是不同关注点。业务代码从阶段 2 起按领域填充，新增功能应放进对应领域包，不要平铺到 `api/` 下。

后续阶段新增的领域包：`files`（知识文档上传与版本链）、`dev_docs`（开发文档前置）、`admin`（全局管理控制台）、`approvals`（审批中心聚合）、`memory`（记忆模块——四层记忆：项目文档索引与检索、成员统计与档案、核心记忆条目与提议确认、历史与经验闭环，含知识库问答；设计文档 `docs/2026-08-16-memory-module-design.md`）。

### infrastructure/：基础设施层

- `database/engine.py`：SQLAlchemy 2 异步引擎（`create_async_engine` + asyncpg 驱动），`pool_pre_ping=True` 让连接池取用连接前先探活（这正是 postgres 重启后 `/health` 能自动恢复 200 的原因之一）。`get_session()` 是 FastAPI 依赖注入用的会话工厂。
- `cache/redis.py`：`create_redis_client()` 工厂函数，基于 `redis.asyncio`，`decode_responses=True`。每次新建客户端而非全局单例，用完 `aclose()`，避免跨事件循环复用连接。
- `queue/queue.py`：Redis List 任务队列（见第 5 节）。
- `storage/`、`integrations/`：目前为空包占位，分别对应第 14 章的 `StorageProvider`（本地文件 → S3/MinIO 演进）和未来的外部集成（第 21 章飞书/Git 平台）。
- `models/base.py`：ORM 基类与 Mixin（第 11 章）：
  - `Base`：所有模型的 `DeclarativeBase`。
  - `UUIDPrimaryKeyMixin`：主键为 PostgreSQL UUID，`server_default=gen_random_uuid()`（由数据库生成，依赖基线迁移启用的 `pgcrypto` 扩展）。
  - `TimestampMixin`：`created_at`/`updated_at`（`server_default=now()`，`onupdate=now()`），对应第 11 章"所有核心表包含 created_at、updated_at"。
  - `VersionMixin`：整型 `version` 字段，用于乐观锁——需要并发保护的表（如 `work_items`，见 17.2 节）额外继承它，更新时携带版本号防止并发覆盖。
  - `CoreModel`：`Base + UUID + Timestamp` 的抽象基类，后续领域模型直接继承；需要乐观锁的再叠加 `VersionMixin`。

### Alembic 迁移机制（T1.3，第 11 章）

- `backend/migrations/env.py`：连接串**不从 `alembic.ini` 读**，而是 `config.set_main_option("sqlalchemy.url", settings.database_url)`，即从统一的 `app.core.config` 拿，保证迁移与应用永远用同一个数据库。`alembic.ini` 里的 `sqlalchemy.url` 只是占位。online 模式用异步引擎 + `connection.run_sync` 执行迁移；`target_metadata = Base.metadata`，后续新增模型后 `alembic revision --autogenerate` 即可识别。
- `migrations/versions/0001_baseline.py`：首个基线迁移，只做一件事——`CREATE EXTENSION IF NOT EXISTS pgcrypto`（UUID 主键的 `gen_random_uuid()` 依赖它）。业务表从阶段 2 的迁移开始建。
- **启动时自动迁移**：`backend/Dockerfile` 的 CMD 是 `alembic upgrade head && uvicorn ...`，容器每次启动先把库迁到最新，重复执行幂等（已在验收中验证：从零初始化成功，重复执行无副作用）。

### 统一错误格式（17.1 节）

API 错误统一为 `{"code", "message", "request_id", "details"}`。阶段 1 已在登录占位路由的 501 响应中按此格式返回；`request_id` 中间件在后续阶段落地（当前为空字符串占位）。前端 `services/api.ts` 已按此约定实现解析。

### 健康检查（T1.4）

`GET /health`（`app/main.py`）检查三项：进程本身、`SELECT 1` 探 PostgreSQL、`PING` 探 Redis，返回 `{"status": "ok|degraded", "checks": {...}}`，全部 ok 才返回 200，否则 503。依赖故障只记异常类型名（`type(exc).__name__`），不把连接串写进日志（连接串里含密码，第 16 章）。

## 4. frontend 说明（T1.5，第 13 章）

### 技术栈

第 13 章要求 React + TypeScript + Vite，推荐 React Router、TanStack Query、轻量全局状态库和管理后台组件库。实际选型（`frontend/package.json`）：

- Vite 6 + React 18 + TypeScript 5
- `react-router-dom` 6（路由）
- `@tanstack/react-query` 5（服务端数据请求/缓存）
- `zustand` 5（轻量全局状态，即"轻量全局状态库"）
- `antd` 5（管理后台组件库）

### 目录结构（第 5 章）

- `src/main.tsx` → `src/app/App.tsx`：组装 `ConfigProvider`（antd 中文 locale）、`QueryClientProvider`、`RouterProvider`。
- `src/app/router.tsx`：路由表，`/login` 独立，其余页面挂在 `AppLayout`（antd Layout + 侧边菜单）下。
- `src/app/store.ts`：zustand 的 `useAuthStore`，只存 `token`/`username`，阶段 2 接入真实登录后填充。
- `src/features/`：七个占位页面（auth、dashboard、members、work-items、approvals、deliverables、agent-assistant），与 13.1/13.2 节的页面规划对应，当前都是 `PlaceholderPage`。
- `src/services/api.ts`：统一 API 客户端（见下）。
- `src/types/index.ts`：统一错误格式 `ApiErrorBody` 等共享类型。

### services/api.ts 的约定

- 所有请求自动拼 `/api/v1` 前缀；开发/生产都走相对路径，由 vite dev server 或 nginx 反代到 backend。
- 自动从 `useAuthStore` 取 token 加 `Authorization: Bearer` 头。
- 响应非 2xx 时：若 body 符合统一错误格式则抛出 `ApiError`（含 `code`/`message`/`requestId`/`details`/`status`）；否则包装成 `HTTP_<status>` 的 `ApiError`。业务代码只需 `try/catch ApiError`。
- 204 响应返回 `undefined`，其余按 JSON 解析。

### Dockerfile 与 nginx

`frontend/Dockerfile` 两阶段构建：第一阶段 `node:20-alpine` 里 `npm install && npm run build`（`NPM_REGISTRY` 构建参数控制 npm 源），产物为 `dist/`；第二阶段 `nginx:1.27-alpine` 拷贝 `dist/` 和 `nginx.conf`，Node 工具链不进入运行镜像。

`nginx.conf` 三条规则：

- `location /`：`try_files ... /index.html` —— SPA 回退，前端路由（如 `/work-items`）刷新时不 404。
- `location /api/`：反代到 `http://backend:8000`，携带真实客户端 IP 头。前端容器因此成为唯一 Web 入口（19.4）。
- `location = /health`：直通 backend 的 `/health`，方便从 Web 端口探活。

## 5. worker / scheduler 说明（T1.6，4.2 节）

### Redis List 队列模型

`infrastructure/queue/queue.py` 用 Redis List 实现最简任务队列：

- 入队 `enqueue()`：把任务（`{id, type, payload, enqueued_at}` 的 JSON）`LPUSH` 到键 `agentos:tasks`。
- 出队 `dequeue()`：`BRPOP` 阻塞读取（默认 5s 超时返回 `None`，让循环有机会刷心跳）。

LPUSH + BRPOP 构成 FIFO。这是首版的最小实现，没有重试/死信——Agent 失败处理（17.3 节）在后续阶段设计。

### Worker（`app/workers/worker.py`）

主循环：刷心跳 → `BRPOP` 取任务 → `handle_task()` 分发。当前只认识 `example.ping` 类型，未知类型记 warning 跳过。

**硬约束（4.2 节）**："Worker 不能直接调用'批准、转派、完成'等业务命令，只能生成 Agent 建议或通知。" 骨架通过不引入任何业务命令代码路径来落实这条约束——`handle_task` 只做日志输出。后续为 Worker 添加能力时，同样只允许写 `agent_suggestions` / `notifications` 这类"建议与通知"出口，状态变更永远由 API 进程内可鉴权、可审计的 REST 命令完成。

### Scheduler（`app/workers/scheduler.py`）

按 `SCHEDULER_EXAMPLE_INTERVAL_SECONDS`（默认 60s）周期触发 `example.ping` 任务（`source=scheduler`）。它是阶段 3/5 的到期提醒、逾期风险扫描、日报等定时任务的**调度挂点**：后续在循环里按同样的"enqueue 一个任务类型"模式注册新调度项即可，实际执行仍由 Worker 消费。

### 心跳保活机制

worker/scheduler 没有 HTTP 端口，Compose 没法用 HTTP 探活，因此采用 Redis 心跳：

- `app/workers/heartbeat.py`：每次循环 `SET agentos:health:<name> <时间戳> EX 30`（30 秒 TTL）。
- `app/workers/healthcheck.py`：Compose healthcheck 执行 `python -m app.workers.healthcheck <name>`，检查对应心跳键是否存在（Redis 不可达视为不健康），退出码 0/1。

进程活着就持续刷新键；进程挂掉 30 秒内键过期，healthcheck 转 unhealthy。这解决了"后台进程存活性信号"的编排需求（T1.4）。

## 6. 数据与配置

### data/ 目录（19.4，不提交 Git）

- `data/postgres/`：PostgreSQL 数据目录（PGDATA 在其下 `pgdata/` 子目录）。
- `data/redis/`：Redis AOF 持久化目录。
- `data/uploads/`：上传文件根目录（第 14 章，数据库只存相对 `storage_key`）。
- `data/backups/`：备份输出目录（每日逻辑备份 + 上传目录增量备份，保留 14 天——备份脚本属后续任务）。
- `data/logs/`：backend/worker/scheduler 三个进程各自的 `<进程名>.log`。

### .env.example 配置项

复制为 `.env` 后 Compose 自动读取；`.env` 不提交 Git，仓库不含真实密钥。

| 配置项 | 含义 |
|---|---|
| `APP_ENV` | 运行环境标识（development/production），写入启动日志 |
| `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` | postgres 容器初始化库名、账号、密码；改密码必须同步改 `DATABASE_URL` |
| `DATABASE_URL` | SQLAlchemy 异步连接串（`postgresql+asyncpg://...`），主机名是 Compose 服务名 `postgres` |
| `REDIS_URL` | Redis 连接串（`redis://redis:6379/0`） |
| `LLM_PROVIDER` | 模型提供方（第 15 章）：`ollama` 或 OpenAI 兼容 |
| `LLM_MODEL` | 模型名（如 `qwen2.5` 等），留空表示未配置 |
| `OLLAMA_BASE_URL` | 宿主机 Ollama 地址，容器内经 `host.docker.internal:11434` 访问 |
| `EMBEDDING_MODEL` / `EMBEDDING_DIMENSIONS` | 记忆模块 embedding 模型与维度（默认 `qwen3-embedding:0.6b` / 1024）；当前 `memory_chunks` 列固定为 1024 维，仅可切换同为 1024 维的模型，切换后须全量重建（命令：`docker compose exec backend python -m app.scripts.rebuild_memory_index --yes`）。变更维度必须先执行专门的数据库迁移，不能仅运行重建脚本。首次部署需在宿主机执行 `ollama pull qwen3-embedding:0.6b`。 |
| `EMBEDDING_PROVIDER` | embedding 提供方：`ollama`（默认，本地）或 `openai_compatible`（智谱等 OpenAI 兼容云端服务——项目文档内容将发送至第三方，16 节数据外发） |
| `FILE_INDEX_LEASE_SECONDS` / `FILE_INDEX_RECOVERY_INTERVAL_SECONDS` | 文档索引租约及 worker 恢复扫描周期（默认 900 秒 / 60 秒）。worker 异常中断后，超过租约的 `indexing` 文件会自动重新投递；只有成功入队才续租，Redis 不可用时下轮继续尝试。 |
| `EMBEDDING_BASE_URL` / `EMBEDDING_API_KEY` | `openai_compatible` 通道的地址与密钥（智谱：`https://open.bigmodel.cn/api/paas/v4` + 控制台 API Key；当前数据库仅支持 1024 维输出） |
| `OPENAI_COMPATIBLE_BASE_URL` / `OPENAI_COMPATIBLE_API_KEY` | OpenAI 兼容云 API 的地址与密钥（可切换的备选通道） |
| `LOG_DIR` | 容器内日志目录，挂载到 `data/logs/` |
| `SCHEDULER_EXAMPLE_INTERVAL_SECONDS` | Scheduler 示例任务触发周期（秒），默认 60 |

## 7. 运维要点

### 误传敏感文件的应急下架流程（记忆模块设计文档 16.3）

知识库文档**严格不可删除**（admin 也不例外），误传含敏感信息的文件时按以下 SOP 在数据库层面对该版本做下架——这是运维操作，不是产品功能：

1. **定位版本**：确认要下架的文件版本与块数量
   ```bash
   docker compose exec postgres psql -U agentos -d agentos -c \
     "SELECT id, original_filename, version, index_status FROM stored_files WHERE original_filename = '<文件名>' ORDER BY version;"
   ```
2. **下架**（检索与问答立即不再命中该版本内容；旧块保留供事后审计）：
   ```sql
   UPDATE memory_chunks SET is_current = FALSE
   WHERE source_type = 'document' AND source_id = '<版本 id>';
   ```
3. **清除文件字节**（可选但建议）：按该行的 `storage_key` 删除 `data/uploads/` 下对应文件（或 MinIO 中对应对象）。
4. **记录**：在团队运维记录中写明操作人、时间、文件名/版本、原因（产品审计域不覆盖运维操作，需人工留痕）。
5. **验证**：用项目内账号调用 `POST /api/v1/memory/search` 与 `/memory/qa` 确认不再命中敏感内容。

注意：操作前先备份或在测试库演练；不要直接删除 `stored_files` 行（会破坏版本链与审计追溯）——确需物理删除时由 DBA 另行评估。

### 镜像源与构建

- 本机 Docker Hub 不可达：基础镜像（`pgvector/pgvector:pg16`、`redis:7-alpine`、`python:3.12-slim`、`node:20-alpine`、`nginx:1.27-alpine`）是从 `docker.m.daocloud.io` 拉取后 `docker tag` 回官方名的。新机器重建时如遇拉取失败，先按此方式处理。
- pip / npm 源通过 compose 的 build args 切换：`backend`/`worker`/`scheduler` 的 `PIP_INDEX_URL`（当前为清华镜像，通用环境改 `https://pypi.org/simple`）、`frontend` 的 `NPM_REGISTRY`（当前为 npmmirror，通用环境改 `https://registry.npmjs.org`）。Dockerfile 内默认值是官方源，compose 里覆盖为国内镜像。
- **buildx 版本要求 ≥ 0.17**（Dockerfile 使用了较新的构建特性）。本机已手动安装 v0.19.3 到 `/root/.docker/cli-plugins/docker-buildx`；如升级 Docker 后插件丢失，需重新安装。

### 常用验证命令

```bash
# 启动 / 查看状态（六服务应全部 healthy；worker/scheduler 依赖心跳，最多等 1-2 分钟）
docker compose up -d
docker compose ps

# 健康检查与占位路由
curl http://localhost:8000/health          # 含 process/postgres/redis 三项
curl http://localhost:8000/api/v1/         # 占位路由 200
curl http://localhost:3000                 # 前端 HTML

# 投递示例任务并观察消费
curl -X POST http://localhost:8000/api/v1/tasks/example
docker compose logs worker --tail 20       # 应看到 consumed example task
docker compose logs scheduler --tail 20    # 每 60s 一条 triggered 日志

# 看日志文件（宿主机）
tail -f data/logs/backend.log data/logs/worker.log data/logs/scheduler.log

# 进容器调试
docker compose exec backend bash
docker compose exec postgres psql -U agentos -d agentos   # 库未对宿主机开放端口，须进容器
docker compose exec redis redis-cli
docker compose exec redis redis-cli keys 'agentos:*'      # 查看队列与心跳键

# 手动迁移（启动时已自动执行；重复执行幂等）
docker compose exec backend alembic upgrade head
docker compose exec postgres psql -U agentos -d agentos -c 'select * from alembic_version'

# 依赖故障演练：停 postgres 后 /health 应返回 503，启动后自动恢复 200
docker stop agentos-postgres && curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8000/health
docker start agentos-postgres
```

### 注意事项

- `docker compose down` 不删 `data/`，数据安全；要重置环境需手动清空 `data/postgres/` 等目录（谨慎操作）。
- 无关的 minio 容器与本项目无关，不属于 Compose 项目，不要动。
- 日志纪律（第 16 章）：任何新代码不得记录密码、令牌、API Key、文件原文；数据库连接串含密码，异常日志只记异常类型。
