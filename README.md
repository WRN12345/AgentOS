# AgentOS

面向小团队的多项目 AI 协作工作流平台。团队可以围绕项目管理成员、工作项、协作、审批、交付物与开发文档，并让专业 Agent 提供需求拆解、人员推荐、计划、风险和审核建议。

> **AI 只提建议，人做决定。** Agent 不直接修改正式业务状态，所有关键流转仍由具备权限的成员确认。

## 当前能力

- **多项目与权限隔离**：用户可参与多个项目并在登录后选择项目；成员、工作项、审批、交付物、通知、审计与 Agent 建议均按项目隔离。
- **平台管理控制台**：全局管理员独立于项目成员体系，负责账号、项目、项目负责人和平台审计管理。
- **项目成员管理**：每个项目仅一名负责人；负责人可添加已有账号、维护成员资料与能力标签，并在项目范围内启用或停用成员。
- **AI 需求拆解**：自然语言需求可生成结构化目标、约束、交付物和验收标准，并进一步拆分工作项、推荐负责人。
- **工作项全流程**：支持草稿、待开始、进行中、阻塞、审核和完成等状态；主执行人唯一，协作、转派和 DDL 变更走申请与审批。
- **开发文档前置**：成员开工前提交开发文档，由 AI 初审并经负责人确认；必要时可由负责人豁免。
- **交付与审核**：提交文件或链接型交付物，负责人进行业务审核，Agent 提供初审清单与风险提示。
- **工作台与团队概览**：展示个人待办、项目统计、团队任务状态、成员负载、通知和审计时间线。
- **Agent 建议中心**：涵盖需求分析、任务规划、人员推荐、开发文档初审、交付初审、进展摘要和周期风险扫描；建议支持查看与采纳闭环。
- **审计、备份与恢复**：关键写操作追加审计记录，提供 PostgreSQL 与上传目录的备份、恢复和校验脚本。

## 角色边界

| 角色 | 主要职责 |
| --- | --- |
| 全局管理员 | 创建和停用账号、创建项目、指定或变更项目负责人、查看平台审计；不参与项目业务协作 |
| 项目负责人 | 管理本项目成员、工作项、审批与审核；确认或拒绝 Agent 建议 |
| 项目成员 | 在所属项目内执行工作项、发起协作、提交开发文档与交付物 |

同一普通账号可以加入多个项目，并在不同项目中拥有独立的成员身份、角色和能力信息。

## 典型流程

1. 全局管理员登录控制台，创建业务账号。
2. 管理员为默认项目指定负责人，或创建新项目并同时指定负责人。
3. 项目负责人将已有账号添加为项目成员，并维护能力与可用工时。
4. 项目负责人创建并发布工作项，成员登录、选择项目后执行被分配的工作项。
5. 开发文档、协作、转派、DDL 变更、交付和审核按工作流推进。
6. Agent 在关键事件或周期扫描中生成建议，由人工决定是否采纳。

## 技术栈

- **后端**：Python 3.12、FastAPI、SQLAlchemy Async、Alembic、PostgreSQL 16、Redis 7
- **Agent**：LangGraph；支持本地 Ollama 或 OpenAI 兼容接口
- **前端**：React 18、TypeScript、Vite、TanStack Query、Zustand、shadcn/ui、Tailwind CSS
- **运行方式**：Docker Compose；后端 API、异步 Worker、Scheduler 与前端独立运行
- **存储**：PostgreSQL 持久化业务数据，本地目录保存上传文件、日志与备份

LLM 不可用时，Agent 运行会记录失败，但账号、项目和工作流等核心业务仍可继续使用。

## 快速开始

### 环境要求

- Docker Engine 或 Docker Desktop
- Docker Compose 插件
- Docker Buildx 0.17 或更高版本

### 1. 创建环境配置

Linux / macOS：

```bash
cp .env.example .env
```

Windows PowerShell：

```powershell
Copy-Item .env.example .env
```

首次启动前必须修改 `.env` 中的 `JWT_SECRET`、`POSTGRES_PASSWORD` 和 `BOOTSTRAP_ADMIN_PASSWORD`。bootstrap 是幂等的，不会用后续环境变量覆盖已存在账号的密码。如果启用 Agent，还需要配置 `LLM_PROVIDER`、`LLM_MODEL` 及对应模型服务地址或密钥。

### 2. 构建并启动

```bash
docker compose up -d --build
docker compose ps
```

如果本机安装的是独立版 Compose，请将上述命令中的 `docker compose` 替换为 `docker-compose`。

Compose 包含六个服务：`postgres`、`redis`、`backend`、`worker`、`scheduler` 和 `frontend`。全部进入 healthy 状态后即可访问：

- Web：http://localhost:3000
- API：http://localhost:8000
- OpenAPI 文档：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

### 3. 首次使用

启动过程会幂等创建：

- `.env` 中 `BOOTSTRAP_ADMIN_*` 指定的全局管理员账号；
- `BOOTSTRAP_PROJECT_NAME` 指定的默认项目。

首次登录后：

1. 在管理控制台创建业务账号，并安全转交仅展示一次的初始密码；
2. 为默认项目指定负责人，或新建项目并指定负责人；
3. 使用负责人账号登录并添加项目成员。

## 常用命令

```bash
# 查看服务状态与日志
docker compose ps
docker compose logs -f backend
docker compose logs -f worker
docker compose logs -f scheduler

# 重建单个服务
docker compose up -d --build backend

# 停止服务（保留数据）
docker compose down

# 手动备份与恢复帮助
deploy/scripts/backup.sh
deploy/scripts/restore.sh --help
```

## 测试与构建

```bash
# 后端测试（测试库会自动执行 Alembic 升级）
docker compose run --rm --no-deps -v "./backend:/app" backend python -m pytest tests/ -q

# 前端测试与生产构建
cd frontend
npm ci
npm test
npm run build
```

## 环境变量

完整配置及注释见 [.env.example](.env.example) 和 [发布指南](docs/release-guide.md)。关键配置包括：

| 配置 | 用途 |
| --- | --- |
| `JWT_SECRET` | JWT 签名密钥，生产环境必须使用强随机值 |
| `POSTGRES_PASSWORD` / `DATABASE_URL` | PostgreSQL 凭据与连接地址 |
| `BOOTSTRAP_ADMIN_*` | 首次启动时幂等创建的全局管理员 |
| `BOOTSTRAP_PROJECT_NAME` | 首次启动时幂等创建的默认项目 |
| `LLM_PROVIDER` / `LLM_MODEL` | Agent 使用的模型提供方与模型 |
| `OLLAMA_BASE_URL` | 容器访问宿主机 Ollama 的地址 |
| `OPENAI_COMPATIBLE_*` | OpenAI 兼容模型服务地址与密钥 |
| `STORAGE_ROOT` / `UPLOAD_*` | 本地文件存储目录和上传限制 |

## 项目结构

```text
backend/    FastAPI 模块化单体、领域服务、Agent、Worker、Scheduler 与 Alembic 迁移
frontend/   React 应用，src/features 按业务功能组织，src/components/ui 为 shadcn/ui 组件
deploy/     备份与恢复脚本
docs/       总体设计、架构指南、专项设计、发布说明与质量基线
data/       PostgreSQL、Redis、上传文件、日志和备份等运行时数据（勿提交）
```

## 文档索引

- [总体设计](docs/2026-07-26-agentos-workflow-platform-design.md)
- [架构指南](docs/architecture-guide.md)
- [发布、部署与迁移指南](docs/release-guide.md)
- [多项目测试接缝与设计决策](docs/2026-08-11-multiproject-testing-seams.md)
- [AI 需求拆解流水线](docs/2026-07-30-agent-requirement-pipeline-design.md)
- [开发文档前置](docs/2026-07-30-dev-doc-gate-design.md)
- [记忆模块设计](docs/2026-08-16-memory-module-design.md)
- [MVP 质量基线（核对/性能/恢复/安全）](docs/quality-baseline-2026-07-29.md)
- [Agent 工作约定](docs/agents/domain.md)
- [Issue tracker 约定](docs/agents/issue-tracker.md)
- [备份与恢复脚本说明](deploy/scripts/README.md)

## 当前限制

- 暂无 SSO，以及 Git、代码托管平台和即时通讯工具的直接集成。
- 暂无用户自助修改或重置密码入口；初始密码需要在创建账号时安全设置和转交。
- 上传文件使用本地目录存储，尚未接入 S3 等对象存储。
- Agent 只生成建议，不直接修改工作项、审批、交付物等正式业务状态。
- Docker Compose 是当前主要部署方式，尚未提供 Kubernetes 部署清单。
