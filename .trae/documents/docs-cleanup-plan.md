# 文档精简整理计划

## Context

AgentOS 当前阶段的开发已基本完成：MVP 13 条标准全部满足（2026-07-29 完成），多项目改造 10 个 ticket 全部已落地到后端与前端代码（包括文档标记为 `ready-for-agent` 的 08/09/10，实际代码显示已实现），GitHub PR 链接校验已合并到 `backend/app/domains/deliverables/schemas.py`。

但文档现状仍停留在"施工期"形态：
- `.scratch/` 残留已完成 spec/ticket 共 12 份（multiproject 10 ticket + spec + PR 校验 spec）
- `docs/` 顶层层累 6 份 phase-X developer-guide（共约 100KB，描述"如何实施某阶段"）
- `docs/tasks/` 累积 8 份任务清单文件，全部已完成
- `docs/` 顶层 4 份分散的质量证据文档（MVP 核对 / 性能基线 / 恢复演练 / 安全检查）

这些文档反映"曾经要做什么"，而当前状态已在根 README 与代码中体现。需要精简到"当前是什么 + 关键决策可溯源"的形态。

**目标**：删除/合并冗余文档，保留设计与运维档案，所有删除内容可通过 git 历史、保留文档或代码本身追溯。

## 已确认的 4 项决策

| # | 范围 | 决策 |
|---|---|---|
| 1 | `.scratch/` | 全部删除（multiproject 决策要点合并进 docs/2026-08-11-multiproject-testing-seams.md） |
| 2 | docs/phase-{1..6}-developer-guide.md | 合并为一份 docs/architecture-guide.md |
| 3 | docs/tasks/ | 整个目录删除（README 索引同步移除 tasks 链接） |
| 4 | docs/{mvp-checklist,perf-baseline,restore-drill,security-checklist}.md | 合并为一份 docs/quality-baseline-2026-07-29.md |

## 执行步骤

### 步骤 1：合并 6 份阶段指南为 architecture-guide.md

**新建** `docs/architecture-guide.md`，从 6 份 phase-X-developer-guide.md 中提取**长期参考价值**的内容（去除施工步骤叙述），章节大纲：

1. **总览** — 模块化单体架构、六服务编排（postgres/redis/backend/worker/scheduler/frontend）
2. **后端结构** — `backend/app/` 目录布局：`core/`（中间件、request_context、idempotency、错误格式）、`api/`（路由层）、`domains/`（领域服务：work_items/members/deliverables/collaboration/transfers/deadlines/notifications/files/agents/audits 等）、`infrastructure/`（db、storage、model_provider、agent_runtime）、`worker/`、`scheduler/`
3. **数据层约定** — SQLAlchemy Async、Alembic 迁移、audit_events 追加式审计、乐观锁（version 字段）、Idempotency-Key 项目化
4. **认证与权限** — Argon2id、Access/Refresh Token（哈希可撤销）、JWT、全局 admin（`users.is_admin`）、`X-Project-Id` 请求头与 contextvars 上下文、对象级越权 404
5. **Agent 子系统** — ModelProvider（Ollama 默认 + OpenAI 兼容，httpx 直连）、LangGraph 五节点图 + PostgreSQL 检查点、统一 Pydantic 输出 Schema、10 项工具护栏（无写业务工具）、ZSET 延迟队列指数退避、六个辅助 Agent
6. **前端结构** — `frontend/src/` 布局：`app/`（store、router、api 客户端自动注入 X-Project-Id、SSE 事件流带 project_id）、`features/`（按业务功能：auth/members/work_items/collaboration/deliverables/agents/admin）、`components/ui/`（shadcn/ui）、TanStack Query + Zustand
7. **测试体系** — 后端五维（单元/API/并发/合约/审计）、前端 Vitest、RAG e2e 串行 + 双任务并行
8. **部署与运维** — Docker Compose 编排、本地文件存储（STORAGE_ROOT/UPLOAD_*）、备份恢复脚本（deploy/scripts/）

**合并完成后删除**：
- `docs/phase-1-developer-guide.md`
- `docs/phase-2-developer-guide.md`
- `docs/phase-3-developer-guide.md`
- `docs/phase-4-developer-guide.md`
- `docs/phase-5-developer-guide.md`
- `docs/phase-6-developer-guide.md`

### 步骤 2：合并 4 份质量证据为 quality-baseline-2026-07-29.md

**新建** `docs/quality-baseline-2026-07-29.md`，四节结构：

1. **MVP 完成标准核对** — 复用 mvp-checklist.md 的 13 条标准表（含测试文件指针与 e2e 场景指针），结论"MVP 于 2026-07-29 完成"
2. **性能基线** — 复用 perf-baseline-2026-07-28.md 的环境（4 核/3.8GB）、101 工作项量级下读 p95 < 35ms / 命令 p95 < 70ms / 登录约 96ms / SSE 建立 < 16ms，含可复测脚本
3. **备份恢复演练** — 复用 restore-drill-2026-07-28.md 的演练链路（造数→backup.sh→恢复→SHA-256 抽查 2/2→安全保护验证→保留策略验证）
4. **安全检查** — 复用 security-checklist-2026-07-29.md 的核查结论（凭据/权限/日志/模型上下文/网络面五维全通过）

**合并完成后删除**：
- `docs/mvp-checklist.md`
- `docs/perf-baseline-2026-07-28.md`
- `docs/restore-drill-2026-07-28.md`
- `docs/security-checklist-2026-07-29.md`

### 步骤 3：合并 multiproject 决策要点到测试接缝文档

**编辑** `docs/2026-08-11-multiproject-testing-seams.md`，在末尾追加一节"## 多项目改造设计决策摘要"，提炼 `.scratch/multiproject/spec.md` 与 10 个 ticket 中的关键决策（不是施工清单）：

- **项目上下文传递**：`X-Project-Id` 请求头 + contextvars 快照（缺失/非成员不强制 4xx，由具体接口决定语义）
- **数据层归属**：6 张独立列表入口表冗余 `project_id`；审批等派生表通过父对象推导归属；同项目校验失败 4xx、越权访问 404
- **全局 admin**：`users.is_admin` 升级为平台级角色，独立于项目成员体系；审计查询对全局 admin 放行
- **幂等键项目化**：同用户同键在不同项目下视为不同请求（`backend/app/core/idempotency.py`）
- **前端项目化**：store 含 currentProject、API 客户端自动注入请求头、SSE URL 带 project_id、登录按 is_admin 分流（admin→/console、用户→项目选择页 24h 记忆）
- **测试接缝**：跨项目隔离在 HTTP API 层验证（pytest+httpx 双项目 fixture），前端 Vitest 验证登录分流与 store 项目化

**编辑完成后删除**：
- 整个 `.scratch/` 目录（含 `multiproject/spec.md`、`multiproject/issues/01-10`、`github-pr-link-validation/spec.md`）

### 步骤 4：删除 docs/tasks/ 整个目录

**删除**：
- `docs/tasks/README.md`
- `docs/tasks/memory-module-implementation-plan.md`
- `docs/tasks/phase-1-foundation.md`
- `docs/tasks/phase-2-identity-members-work-items.md`
- `docs/tasks/phase-3-collaboration.md`
- `docs/tasks/phase-4-delivery-review.md`
- `docs/tasks/phase-5-agent-assistance.md`
- `docs/tasks/phase-6-quality-deployment.md`

理由：任务清单是待办工件，全部已完成；任务编号与对应设计章节的映射可在 `docs/2026-07-26-agentos-workflow-platform-design.md` 第 20 章找到。

### 步骤 5：更新根 README.md 文档索引

**编辑** `README.md` 的 `## 文档索引` 一节，更新链接列表为精简后的实际文档：

```markdown
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
```

同时更新 `## 项目结构` 中 `docs/` 的描述（移除"阶段指南"字样，改为"设计与架构指南"）。

### 步骤 6：保留不动的文档

以下文档**不动**：
- `docs/2026-07-26-agentos-workflow-platform-design.md` — 平台总设计（22 章），长期参考
- `docs/2026-07-30-agent-requirement-pipeline-design.md` — 专项设计
- `docs/2026-07-30-dev-doc-gate-design.md` — 专项设计
- `docs/2026-08-11-multiproject-testing-seams.md` — 专项设计（步骤 3 追加决策摘要）
- `docs/2026-08-16-memory-module-design.md` — 专项设计
- `docs/release-guide.md` — 运维必需
- `docs/agents/domain.md`、`docs/agents/issue-tracker.md`、`docs/agents/triage-labels.md` — Agent 工作约定（issue-tracker.md 中 `.scratch/` 约定保留，作为未来新 feature 的约定，当前目录为空）

## 关键文件清单

**新建**（2 份合并文档）：
- `docs/architecture-guide.md`
- `docs/quality-baseline-2026-07-29.md`

**编辑**（2 份）：
- `docs/2026-08-11-multiproject-testing-seams.md` — 追加多项目设计决策摘要
- `README.md` — 更新文档索引与项目结构描述

**删除**（共 22 份）：
- `.scratch/` 整个目录（12 份：multiproject spec + 10 ticket + PR 校验 spec）
- `docs/phase-{1..6}-developer-guide.md`（6 份）
- `docs/tasks/` 整个目录（8 份）
- `docs/mvp-checklist.md`、`docs/perf-baseline-2026-07-28.md`、`docs/restore-drill-2026-07-28.md`、`docs/security-checklist-2026-07-29.md`（4 份）

## 可溯源性保证

所有删除内容均可通过以下途径追溯：
1. **git 历史**：所有原文件保留在 git 历史中，可通过 commit hash 或文件名追溯
2. **代码本身**：实施结果体现在代码中（如 multiproject 改造体现在 `backend/app/core/middleware.py`、`backend/app/core/request_context.py`、各 domain models 的 `project_id` 字段；PR 校验体现在 `backend/app/domains/deliverables/schemas.py`）
3. **保留文档**：关键设计与决策体现在保留的文档中（总设计、专项设计、架构指南、测试接缝文档的多项目决策摘要、质量基线）
4. **根 README**：当前能力概述

## 验证方法

1. **文件清单核对**：执行 `Get-ChildItem -Path docs,.scratch -Recurse -File` 确认最终文件列表符合预期（.scratch 不存在，docs/ 顶层文档数量从 14 份降至 8 份，docs/tasks/ 不存在，docs/agents/ 保留 3 份）
2. **链接有效性**：在 README.md 文档索引中逐条点击链接，确认目标文件存在
3. **内容完整性核对**：
   - architecture-guide.md 覆盖原 6 份 phase-X 的关键架构信息（后端结构、前端结构、Agent 子系统、测试体系、部署运维）
   - quality-baseline-2026-07-29.md 包含原 4 份的全部测试指针与数字证据
   - 2026-08-11-multiproject-testing-seams.md 末尾的决策摘要覆盖 spec 的 6 类关键决策
4. **git 状态**：`git status` 显示删除的文件与新增的 2 份合并文档，无意外变更
