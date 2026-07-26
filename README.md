# AgentOS

单项目智能协作与留痕平台。FastAPI + React + PostgreSQL + Redis，Docker Compose 部署。

- 设计方案：[docs/2026-07-26-agentos-workflow-platform-design.md](docs/2026-07-26-agentos-workflow-platform-design.md)
- 任务拆分：[docs/tasks/README.md](docs/tasks/README.md)
- 开发者指南：[docs/phase-1-developer-guide.md](docs/phase-1-developer-guide.md)

## 快速开始

要求：Docker + Compose 插件，buildx ≥ 0.17。

```bash
cp .env.example .env
docker compose build
docker compose up -d
```

## 依赖

- 后端：`backend/pyproject.toml`、`backend/requirements.txt`
- 前端：`frontend/package-lock.json`（Dockerfile 用 `npm ci`）
