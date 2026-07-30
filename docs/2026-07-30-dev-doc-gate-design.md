# 开发文档前置（先文档后开发）设计

日期：2026-07-30
状态：待评审

## 1. 需求

每个任务的主执行人在开始开发前，必须先留下该任务的开发文档（设计思路、实现方案、接口约定、排期等），否则不能开工。目的：方案先于代码、过程可追溯、负责人能在动手前纠偏。

## 2. 关键决策

| 决策点 | 结论 | 说明 |
|---|---|---|
| 强制程度 | 强制：`READY → IN_PROGRESS` 状态迁移时校验，文档未确认通过则拦截 | 已确认。与现有状态机/审计体系一致 |
| 文档形式 | 在线 Markdown 编辑，存数据库文本字段 | 默认采纳。可版本留痕、AI 可直接读取；不上传文件 |
| 归属粒度 | 每个任务一份，主执行人撰写；协作者可查看 | 默认采纳 |
| 审核环节 | 提交后 Agent 自动初审（建议性质）→ 负责人确认/打回；确认通过才放行开工 | 默认采纳，符合"人类决定，Agent 建议"原则 |

## 3. 流程

```
任务 READY（已分配主执行人）
  → 主执行人在任务详情页"开发文档"区撰写/编辑（草稿可反复保存）
  → 点击"提交审核"：文档锁定为待确认状态，触发 Agent 初审（dev_doc_review）
  → Agent 产出初审建议（完整性、与验收标准对齐度、风险提示，写入 agent_suggestions）
  → 负责人在审批中心看到"开发文档确认"条目（附 AI 初审意见链接），确认通过或打回
  → 确认通过：文档锁定归档，任务允许 start（READY → IN_PROGRESS）
  → 打回：附理由，文档回到可编辑状态，成员修改后重新提交
```

豁免：负责人创建/分配的纯管理类任务如需跳过，负责人在任务详情页可"豁免文档要求"（写审计日志）。默认不豁免。

## 4. 后端设计

### 4.1 数据模型（需 alembic 迁移）

新表 `dev_docs`：

| 字段 | 类型 | 说明 |
|---|---|---|
| id | UUID | 主键 |
| work_item_id | UUID FK | 唯一，一个任务一份 |
| author_member_id | UUID FK | 撰写人（提交时的主执行人） |
| content | Text | Markdown 正文 |
| status | String | `DRAFT` / `SUBMITTED` / `CONFIRMED` / `RETURNED` |
| review_note | Text | 负责人打回理由（可空） |
| confirmed_by / confirmed_at | UUID / DateTime | 确认人与时间（可空） |
| doc_version | Integer | 每次提交 +1，历史快照另存 `dev_doc_versions`（可选，P2） |
| created_at / updated_at | DateTime | 常规 |

状态机：`DRAFT → SUBMITTED → CONFIRMED`，`SUBMITTED → RETURNED → DRAFT(可再编辑)`。

### 4.2 接口

- `GET /work-items/{id}/dev-doc`：任务相关成员可读（含 AI 初审建议关联）
- `PUT /work-items/{id}/dev-doc`：仅主执行人，DRAFT/RETURNED 状态可编辑（带乐观锁，沿用现有 version 模式）
- `POST /work-items/{id}/dev-doc/submit`：仅主执行人；内容非空校验；触发 `dev_doc_review` Agent run（trigger_source=event）
- `POST /work-items/{id}/dev-doc/confirm`、`POST /work-items/{id}/dev-doc/return`：仅负责人；写审计
- `POST /work-items/{id}/dev-doc/waive`：仅负责人，豁免该任务文档要求（写审计）

### 4.3 状态机拦截

`work_items/service.py` 的 `start` 命令执行前校验：该任务存在 `status=CONFIRMED` 的 dev_doc，或已被豁免；否则返回 409 + 明确文案"请先提交开发文档并通过负责人确认"。`unblock`（BLOCKED → IN_PROGRESS）不重复校验。

### 4.4 Agent 初审（复用现有建议体系）

- 新增专家能力 `dev_doc_review`（`app/agents/specialists/` + prompts，版本 `dev_doc_review.v1`）：输入文档正文 + 任务标题/说明/验收标准，输出 `checklist[]`（完整性检查：目标/方案/接口/排期/风险）、`alignment`（与验收标准对齐度）、`risks[]`、`verdict`（sufficient / needs_work）
- 输出写入 `agent_suggestions`（新 suggestion_type=`dev_doc_review`），关联 work_item_id；只建议不决策，护栏不动
- 事件触发：submit 时入队（复用 deliverable_review 的 event 触发模式）

### 4.5 审批中心集成

- `GET /approvals` 聚合新增第三类：`kind="dev_doc"`，数据来源 `dev_docs.status=SUBMITTED`，summary 为"任务标题 + 第 N 次提交"
- `GET /approvals/processed` 同步纳入 CONFIRMED/RETURNED 记录（复用上次的审批历史机制）

## 5. 前端设计

- **任务详情页**新增"开发文档"Section：
  - 成员视角：Markdown 编辑器（textarea + 预览即可，不引富文本库）、保存草稿、提交审核；被打回时显示理由；SUBMITTED/CONFIRMED 状态只读展示 + AI 初审意见面板
  - 负责人视角：查看文档 + AI 初审意见 + 确认/打回/豁免按钮
- **审批中心**：待我审批列表新增"开发文档"类型条目（带 AI 初审 verdict 标识），点击展开文档内容与初审意见，确认/打回；审批记录页同步展示历史
- **拦截提示**：成员点"开始开发"被 409 拦截时，toast 引导到文档编辑区
- **工作台**：统计卡/待办中对"待交文档"的任务给标记（可选增强，P2）
- AI 助手页建议类型元数据加 `dev_doc_review`（"文档初审"）

## 6. 改动范围清单

后端：
- `app/domains/dev_docs/`（新域：models / schemas / service / router / state_machine）
- `migrations/versions/`：新迁移（dev_docs 表）
- `app/domains/work_items/service.py`：start 前置校验
- `app/domains/approvals/service.py`：聚合第三类 + 历史纳入
- `app/agents/specialists/dev_doc_review.py` + `prompts/` + graphs 注册 + schemas 载荷校验
- 测试：`tests/test_dev_docs_api.py`（CRUD/状态机/权限/拦截）、Agent 契约与护栏用例

前端：
- `features/work-items/`：DevDocSection 组件 + 详情页挂载 + start 拦截提示
- `features/approvals/`：新 kind 渲染
- `features/agent-assistant/constants.ts`、`SuggestionContent.tsx`：新建议类型
- `types/index.ts`：DevDoc 类型

## 7. 验收标准

1. 主执行人不交文档点"开始开发"被拦截，文案明确；负责人豁免后可开工。
2. 提交文档后负责人能在审批中心看到条目和 AI 初审意见；确认后任务可 start；打回后成员可修改重交。
3. 全部状态变更写审计；Agent 不产生业务写入（护栏测试通过）。
4. LLM 不可用时：文档提交/确认正常，仅初审建议缺失（降级，不阻塞流程）。

## 8. 备注

- P2 可选：文档历史版本快照（`dev_doc_versions`）、工作台"待交文档"标记、豁免率统计。
- 与交付物（deliverables）的关系：开发文档管"开工前"，交付物管"完工后"，两者独立不合并。
