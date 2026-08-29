# AgentOS 内部发布指南（T6.7）

本文面向需要在内部 Debian 机器上部署或开发 AgentOS 的工程师。章节号指设计文档
`docs/2026-07-26-agentos-workflow-platform-design.md`。

## 1. 宿主机准备（19.1 节）

- Debian 64 位系统（bookworm 或更新）。
- Git：`sudo apt install git`
- Docker Engine、Buildx 与 Compose Plugin：按 Docker 官方 Debian 源安装
  `docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin`。
- Ollama（Agent 辅助功能需要）：以宿主机服务运行，`curl -fsSL https://ollama.com/install.sh | sh`，
  然后拉取模型，如 `ollama pull qwen2.5:7b`。**Ollama 不可用时核心工作流不受影响**
  （第 22 章标准 9），只是 Agent 建议全部落 failed。
- 快速开发模式额外安装：Node.js LTS（≥20）、Python ≥3.12、`uv`（可选）。

## 2. 获取代码与配置

```bash
git clone <仓库地址> AgentOS && cd AgentOS
cp .env.example .env   # 然后按下表逐项修改
```

### 2.1 配置项说明（.env 逐项）

| 变量 | 说明 | 生产注意 |
| --- | --- | --- |
| `APP_ENV` | 运行环境标识（development/production） | 生产设 production |
| `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` | 数据库名、用户、密码 | 必须改强密码 |
| `DATABASE_URL` | SQLAlchemy 连接串（容器内主机名 `postgres`） | 密码与上行一致 |
| `REDIS_URL` | Redis 连接串（容器内主机名 `redis`） | 默认即可 |
| `LLM_PROVIDER` | 模型提供方：`ollama` 或 `openai_compatible` | — |
| `LLM_MODEL` | 模型名（如 `qwen2.5:7b`）；留空时 Agent 运行会落 failed 并提示配置 | 按已拉取模型填 |
| `OLLAMA_BASE_URL` | 容器经 host-gateway 访问宿主机 Ollama：`http://host.docker.internal:11434` | 默认即可 |
| `OPENAI_COMPATIBLE_BASE_URL` / `OPENAI_COMPATIBLE_API_KEY` | 外部 OpenAI 兼容服务地址与 Key（16 节：外部模型时前端会提示数据外发） | Key 只进 .env |
| `LLM_TIMEOUT_SECONDS` / `LLM_MAX_RETRIES` | 单次模型调用超时与线性重试次数（17.3 节） | — |
| `LLM_MAX_TOKENS` | 单次生成最大 token 数（OpenAI 兼容 Provider 生效；推理模型 thinking 也占额度，默认 4096） | 输出被截断时调大 |
| `AGENT_RUN_MAX_RETRIES` / `AGENT_RUN_RETRY_BASE_SECONDS` | Agent 运行级指数退避重试：间隔 = base × 2^attempt | — |
| `LOG_DIR` | 容器内日志目录（挂载 `./data/logs`） | 默认即可 |
| `SCHEDULER_EXAMPLE_INTERVAL_SECONDS` | Scheduler 示例任务周期 | 默认即可 |
| `DUE_SCAN_INTERVAL_SECONDS` | 到期/逾期提醒扫描周期（秒） | 默认 300 |
| `AGENT_RISK_SCAN_INTERVAL_SECONDS` | 风险扫描 Agent 周期（秒） | 默认 86400（24 小时） |
| `STORAGE_BACKEND` / `STORAGE_ROOT` | 文件存储后端（首版 `local`）与容器内根目录 | 默认即可 |
| `UPLOAD_MAX_BYTES` / `UPLOAD_ALLOWED_EXTENSIONS` / `UPLOAD_ALLOWED_MIME_TYPES` | 上传大小上限与类型白名单 | 按需收紧 |
| `JWT_SECRET` | JWT 签名密钥 | **必须换强随机值** |
| `ACCESS_TOKEN_EXPIRE_MINUTES` / `REFRESH_TOKEN_EXPIRE_DAYS` | 令牌有效期 | — |
| `BOOTSTRAP_ADMIN_USERNAME` / `BOOTSTRAP_ADMIN_PASSWORD` | 初始负责人账号（容器启动时幂等创建） | **首次登录后立即改密** |
| `BOOTSTRAP_PROJECT_NAME` / `BOOTSTRAP_ADMIN_DISPLAY_NAME` | 默认项目名与负责人显示名 | — |

## 3. 标准模式：Docker Compose 一键启动（19.3 节）

```bash
docker compose build
docker compose up -d
docker compose ps        # 六个服务全部 healthy 即就绪
```

- 前端：http://localhost:3000 （后端 API：http://localhost:8000 ，`/health` 健康检查）
- backend 启动时自动执行 `alembic upgrade head` 与初始账号引导（幂等）。
- postgres / redis 不发布端口，仅 Compose 内网可达（19.4 节）；Ollama 在宿主机。
- 数据持久化在 `./data/`（postgres / redis / uploads / backups / logs），备份见第 5 节。

## 4. 快速开发模式（19.3 节）

只让 PostgreSQL 和 Redis 跑在 Docker，应用进程在宿主机跑（热重载）：

```bash
docker compose up -d postgres redis

# 后端（Python ≥3.12，建议使用 uv/venv）
cd backend && uv sync --extra dev   # 或 pip install -e ".[dev]"
DATABASE_URL=postgresql+asyncpg://agentos:<密码>@localhost:5432/agentos \
REDIS_URL=redis://localhost:6379/0 \
  sh -c 'alembic upgrade head && python -m app.scripts.bootstrap && uvicorn app.main:app --reload'

# Worker / Scheduler（另开终端，同环境变量）
python -m app.workers.worker
python -m app.workers.scheduler

# 前端（另开终端）
cd frontend && npm install && npm run dev
```

注意：快速模式需要 postgres/redis 对宿主机可见——本仓库 compose 默认不发布端口，
快速模式时请自行临时加 `ports: ["127.0.0.1:5432:5432"]` / `["127.0.0.1:6379:6379"]`
（只绑回环，勿对公网开放），或用 `docker compose exec`/`run` 在网络内操作。

## 5. 备份与恢复（19.4 节，T6.5）

- 每日备份：`deploy/scripts/backup.sh`（PostgreSQL 逻辑备份 + 上传目录增量备份 +
  14 天保留清理，日志写 `data/logs/backup.log`）。
- 恢复：`deploy/scripts/restore.sh --dump <文件> --target-db <库名> [--uploads-archive ...]`，
  覆盖主库需显式 `--confirm`；恢复后自动校验（连通性、核心表、文件 SHA-256 抽查）。
- 定时任务（宿主机 crontab）与增量恢复细节见 `deploy/scripts/README.md`。
- 恢复演练记录：`docs/quality-baseline-2026-07-29.md` 第 3 节（MVP 标准 12 证据，每月至少一次）。

## 6. 测试入口

- 后端：`docker compose run --rm --no-deps -v "$PWD/backend:/app" backend python -m pytest tests/ -q`
  （独立 `agentos_test` 库 + Redis db15，自动建库迁移，不污染主库）
- 前端：`cd frontend && npm run test`（Vitest，57 例）
- 端到端验收场景：`pytest tests/test_e2e_rag_serial.py tests/test_e2e_parallel.py`
- 性能基线复测：见 `docs/quality-baseline-2026-07-29.md` 第 2 节

## 7. 角色模型（2026-07-29 起）

三种角色（`project_members.role`）：

| 角色 | 定位 | 能力 |
| --- | --- | --- |
| 管理员 admin | 领导/系统管理（初始引导账号即此角色） | 只读全部页面（看板、工作项、审批列表、交付物、审计、Agent 建议）+ 成员账号管理（创建/编辑/禁用/能力）；**不能**创建/分配工作项、审批、审核、参与协作，**不能被指派**（422「管理员不参与工作协作」） |
| 负责人 leader | 项目日常负责人（由管理员在"成员与能力"页创建） | 创建/分配工作项、审批转派与 DDL、审核交付物、Agent 建议反馈等全部业务操作 |
| 成员 member | 执行人 | 看板、我的任务、协作、提交交付 |

首次部署后流程：admin 登录 → 创建一名"负责人"角色成员 → 负责人登录开展业务。

## 8. 已知限制（2.2 节首版不包含）

- 单项目、无 SSO、无多项目/跨项目成员。
- 无 GitProvider/NotificationProvider 集成：Git 链接由成员手工粘贴，飞书同步为系统外
  手工步骤（平台以终态归档 + 证据文件留痕）。
- 无 WebSocket（实时推送用 SSE）、无 MinIO/对象存储（本地文件存储，预留 StorageProvider 抽象）。
- Agent 只能生成建议，不能改变任何正式业务状态（原则 2，标准 10）。

## 9. 常见问题

- **Agent 建议全部 failed**：通常是 `LLM_MODEL` 未配置或宿主机 Ollama 未运行/未拉模型；
  核心工作流不受影响。用 echo 占位能力可无模型自检管道。
- **容器内访问不到宿主机 Ollama**：确认宿主机 `ollama serve` 监听 11434，且 compose 中
  `extra_hosts: host.docker.internal:host-gateway` 存在（Linux 必需）。
- **端口冲突**：宿主机 8000/3000 被占用时改 compose 的 ports 映射左侧端口；
  切勿给 postgres/redis 加对公网端口映射。
- **初始账号**：`BOOTSTRAP_ADMIN_USERNAME/PASSWORD` 仅首次引导时生效（幂等），
  改 .env 不会重置已存在账号的密码。

## 10. 部署验证记录（第 22 章标准 11）

2026-07-29 于开发机执行：`docker compose build` 全部镜像构建成功 →
`docker compose down && docker compose up -d` → 六服务全部 healthy →
`GET /health` 返回 200 → 前端 3000 可打开 → `POST /api/v1/auth/login`
（admin）返回令牌成功。验证人：Kimi Code CLI。
