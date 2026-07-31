# AgentOS

面向小团队的 AI 协作工作流平台：用自然语言描述需求，AI 拆解成任务并按成员技能推荐负责人，人工确认后执行；任务流转、审批、交付、开发文档全程留痕。**AI 只提建议，人做决定。**

## 功能概览

- **AI 需求拆解**：一句话需求 → 自动分析涉及方面 → 拆解为多个任务并预填字段 → 按成员技能/负载推荐负责人（也支持文中直接点名）→ 负责人确认后批量创建
- **任务全流程**：草稿 → 待开始 → 进行中 → 阻塞/审核 → 完成，主执行人唯一，协作/转派/DDL 变更走申请审批
- **开发文档前置**：成员开工前必须提交开发文档，AI 初审 + 负责人确认通过才能开始开发（可豁免）
- **审批中心**：转派、DDL 变更、开发文档集中审批，处理结果留痕可查
- **交付物与审核**：交付物提交、负责人审核、AI 初审清单辅助
- **工作台与团队概览**：个人待办/统计卡/AI 动态，团队任务状态分布与成员负载图表
- **Agent 建议体系**：风险扫描（每日）、进展摘要、交付初审等六类专家 Agent，建议-采纳闭环，护栏保证 AI 不写业务数据
- **审计与备份**：业务写入全量审计日志，每日备份 + 一键恢复脚本

## 技术栈

FastAPI + SQLAlchemy(async)+ PostgreSQL + Redis + LangGraph(Agent 编排）| React + Vite + TanStack Query + shadcn/ui | Docker Compose 部署 | LLM 走 Ollama（本地）或 OpenAI 兼容接口（云端），不可用时核心流程不受影响

## 快速开始

要求：Docker + Compose 插件（buildx ≥ 0.17）。

```bash
cp .env.example .env   # 按需修改，JWT_SECRET 必须换强随机值
docker compose build
docker compose up -d
docker compose ps      # 六个服务全部 healthy 即就绪
```

- 前端：http://localhost:3000 ；后端 API：http://localhost:8000（`/health` 健康检查）
- 初始账号由 `.env` 的 `BOOTSTRAP_ADMIN_*` 在首次启动时幂等创建，**首次登录后立即改密**
- 首次使用流程：admin 登录 → 成员与能力页创建"负责人" → 负责人登录开展业务

## 项目结构

```
backend/    FastAPI 应用（app/domains 业务域、app/agents Agent 系统、app/workers 队列）
frontend/   React 应用（src/features 按功能模块组织）
deploy/     备份/恢复脚本
docs/       设计文档、阶段开发者指南、任务拆分
data/       运行时数据（postgres/redis/uploads/backups/logs，勿提交）
```

## 常用命令

```bash
docker compose logs -f backend          # 看后端日志（worker/scheduler 同理）
docker compose up -d --build backend    # 改代码后重建并重启某服务
deploy/scripts/backup.sh                # 手动备份（数据库 + 上传目录）
deploy/scripts/restore.sh --help        # 恢复（需显式 --confirm）

# 测试
cd frontend && npm test                 # 前端 Vitest
docker compose run --rm --no-deps -v "$PWD/backend:/app" backend python -m pytest tests/ -q
```

## 环境变量

全部配置项及说明见 `.env.example`（注释逐项标注）与 `docs/release-guide.md` §2.1。
关键项：`JWT_SECRET`（必换）、`POSTGRES_PASSWORD`（必换）、`LLM_PROVIDER`/`LLM_MODEL`（Agent 功能，不配置则建议全部 failed 但不影响核心流程）、`BOOTSTRAP_ADMIN_*`（初始账号）。

## 文档

- 总体设计：[docs/2026-07-26-agentos-workflow-platform-design.md](docs/2026-07-26-agentos-workflow-platform-design.md)
- 发布/部署/迁移指南：[docs/release-guide.md](docs/release-guide.md)
- 阶段开发者指南：[docs/phase-1-developer-guide.md](docs/phase-1-developer-guide.md)（同目录 phase-2 ~ 6）
- 任务拆分：[docs/tasks/README.md](docs/tasks/README.md)
- 近期特性设计：[AI 需求拆解流水线](docs/2026-07-30-agent-requirement-pipeline-design.md)、[开发文档前置](docs/2026-07-30-dev-doc-gate-design.md)

## 已知限制

单项目、无 SSO、无 Git/IM 集成、无对象存储（本地文件存储）；Agent 只能生成建议，不能改变任何正式业务状态。
