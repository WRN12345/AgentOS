# Agent 需求拆解流水线设计（需求 → 拆解 → 分配 → 确认）

日期：2026-07-30
状态：待评审

## 1. 背景与问题

现有 Agent 系统（`backend/app/agents/`，Phase 5 落地）是"建议型"架构：六个专家 Agent（requirement_analyst / assignment_advisor / planning_advisor / workflow_risk / deliverable_review / summary_agent）各自独立触发，产出 `agent_suggestions` 建议记录，由负责人在建议中心采纳/忽略。

实际使用中的问题：

- **三个环节割裂**：需求分析、任务拆解（planning）、分配建议（assignment）是三条独立的建议，负责人需要分别触发、分别阅读、分别采纳，没有串成一条可用的流程。
- **拆解结果无法直接落地**：planning 建议里的 `work_item_breakdown[]` 只在建议卡片中展示，不能一键生成工作项；现有的 `RequirementGuidedCreateDialog` 只调用了 requirement_analyst，且只支持创建**单个**工作项。
- **未利用成员技能数据**：成员能力标签/熟练度/负载数据已完整（`member_capabilities`、`weekly_available_hours`），但分配建议需要单独触发，没有出现在创建流程里。

结论：基础设施（LLM 接入、LangGraph 图、建议 Schema、护栏、确认闭环）已经齐全，缺的是**把三个专家 Agent 编排成一条流水线 + 前端向导式确认界面**。

## 2. 目标

一条一体化向导流水线：

```
负责人输入自然语言需求（可指定人选）
  → Agent 分析需求涉及的方面（领域/技术点）
  → Agent 拆解为多个工作项（标题/说明/验收标准/优先级/DDL）
  → Agent 按成员技能+负载推荐负责人；用户指定的人选优先，Agent 做合理性校验
  → 预填完整的多工作项表单
  → 负责人逐项确认/修改
  → 批量创建正式工作项
```

坚守既有原则：**人类决定，Agent 建议**（总设计 §3 原则 2）。Agent 不做任何业务写入。

## 3. 关键决策

| 决策点 | 结论 | 说明 |
|---|---|---|
| 流程形态 | 一体化向导流水线 | 已确认。组合现有三个专家 Agent，不做对话式多轮交互 |
| 落地方式 | 拆解结果存 `agent_suggestions`，负责人确认后由前端调正式 `POST /work-items` 批量创建 | 已确认。沿用现有护栏与审计语义，零风险 |
| 分配方式 | 两者都支持：Agent 按技能/熟练度/负载自动推荐；用户可在需求文本中指定（如"后端给张三"），Agent 尊重指定并做合理性校验提示 | 默认采纳 |
| 拆解粒度 | 工作项 + 协作点 + 建议 DDL | 复用现有 `work_item_breakdown[]` / `collaboration_points[]` 输出结构 |
| 旧对话框 | `RequirementGuidedCreateDialog` 下线，统一入口为新向导 | 默认采纳，不留"单工作项快捷模式" |
| 任务依赖 | 本期不做前置依赖字段，仅展示协作点 | 默认采纳 |
| 涉及方面 | `involved_aspects` 复用成员技能标签词表（取自 `member_capabilities.tag` 去重集合），Agent 从词表中挑选 | 默认采纳，保证分配匹配准确 |

## 4. 后端设计

### 4.1 新增组合能力 `requirement_pipeline`

在 `app/agents/graphs/base.py` 的 `CAPABILITIES` / `AGENT_ROUTES` 中注册新的 `agent_type="requirement_pipeline"`，实现于 `app/agents/specialists/pipeline.py`。它不是第四套独立逻辑，而是**在同一个 run 内顺序编排**三个现有专家能力：

1. **需求分析**（复用 requirement_analyst）：提取目标/约束/交付物/验收标准，并新增输出 `involved_aspects[]`（需求涉及的方面/技术点，用于驱动分配）。
2. **拆解**（复用 planning_advisor）：产出 `work_item_breakdown[]`，每项含 title/description/acceptance_criteria/priority/suggested_due_at + `collaboration_points[]`。
3. **分配**（复用 assignment_advisor 的数据查询，即 `tools.py` 的成员能力/负载只读工具）：为每个拆解项给出 `recommended_assignee` + `candidates[]` + 理由 + fact_refs（真实 member_id）。

指定人选处理：需求文本中点名的人选（按 display_name/username 模糊匹配到成员）作为 hard constraint 传入分配步骤；Agent 不更改指定，只在 `assignment_notes` 中给出合理性提示（如技能不匹配、负载过高）。匹配不到的名字列入 `unresolved_mentions[]` 并在表单中醒目标出。

### 4.2 建议输出 Schema 扩展

`AgentSuggestionOutput` 的 content 增加 `requirement_pipeline` 类型的载荷（复用现有字段，新增少量）：

```json
{
  "goals": [], "constraints": [], "deliverables": [], "acceptance_criteria": [],
  "involved_aspects": ["RAG", "FastAPI"],
  "work_item_breakdown": [
    {
      "title": "...", "description": "...", "acceptance_criteria": "...",
      "priority": "P1", "suggested_due_at": "2026-08-10",
      "recommended_assignee": {"member_id": 3, "display_name": "张三", "reason": "..."},
      "candidates": [{"member_id": 5, "display_name": "李四", "reason": "..."}],
      "user_specified": false
    }
  ],
  "collaboration_points": [],
  "unresolved_mentions": [],
  "risks": []
}
```

提示词版本：`requirement_pipeline.v1`（`app/agents/prompts/`）。

### 4.3 接口

复用现有端点，无需新增路由：

- 触发：`POST /api/v1/agent-analysis`，body `{agent_type: "requirement_pipeline", prompt: "<需求原文>"}`（仅 leader，沿用现有权限）。
- 轮询：`GET /api/v1/agent-runs/{id}`。
- 反馈：`POST /api/v1/agent-suggestions/{id}/feedback`（accepted/ignored）。
- 创建：确认后前端逐项调既有 `POST /api/v1/work-items`（自动带 Idempotency-Key），全部成功后写 `accepted` 反馈；部分失败时保留建议供重试。

### 4.4 不变的部分

- 护栏：`FORBIDDEN_OPERATIONS` 不放宽，Agent 仍只写建议表；`tests/test_agent_guardrails.py` 增加 pipeline 类型的护栏用例。
- LangGraph 五节点基础图、检查点、重试、SSE 事件（`agent.suggestion_ready`）全部复用。
- 云端模型警示（`llm_is_external`）在向导中继续展示。

## 5. 前端设计

新组件 `RequirementPipelineWizard.tsx`（`frontend/src/features/agent-assistant/`），取代 `RequirementGuidedCreateDialog` 在工作项页和 Agent 助手页的入口，旧对话框下线。

四步向导：

1. **输入需求**：多行文本框 + 提示"可在文中直接指定人选，如：接口部分给张三"；显示外部模型数据外发警示。
2. **等待分析**：触发后轮询 run 状态（沿用 2s 轮询逻辑），失败可返回修改需求或手动重试。
3. **确认拆解结果**：
   - 顶部展示涉及方面 `involved_aspects[]` 与整体目标/约束/风险；
   - 下方为工作项卡片列表，每项可编辑：标题、说明、验收标准、优先级、DDL、主执行人（下拉排除 admin，Agent 推荐项置顶并标注理由；用户指定的项标注"按需求指定"，有合理性提示时显示警告图标）；
   - 支持删除某项、手动新增一项；
   - `unresolved_mentions[]` 非空时顶部横幅提示。
4. **批量创建**：逐项调 `POST /work-items`，展示每项结果；全部成功后写 accepted 反馈并跳转工作项列表；点"忽略"仅写 ignored 反馈。

协作点 `collaboration_points[]` 本期仅展示，不自动创建协作请求（协作请求走既有流程）。

## 6. 改动范围清单

后端：
- `app/agents/specialists/pipeline.py`（新增）
- `app/agents/prompts/pipeline.py`（新增）
- `app/agents/graphs/base.py`：注册 `requirement_pipeline`
- `app/agents/schemas/suggestion.py`：pipeline 载荷校验
- `app/agents/router.py`：`agent_type` 白名单加入新类型
- 测试：`tests/test_agent_pipeline.py`（契约 + 指定人选解析 + 护栏）

前端：
- `features/agent-assistant/RequirementPipelineWizard.tsx`（新增）
- `SuggestionContent.tsx`：渲染 pipeline 类型建议
- `constants.ts`：新增类型标签
- `WorkItemsPage.tsx` / `AgentAssistantPage.tsx`：替换入口
- `types/index.ts`：pipeline 载荷类型

数据库：**无迁移**（复用 agent_suggestions.content JSON 字段）。

## 7. 验收标准

1. 负责人输入一段自然语言需求，可在一个向导内得到拆解后的多个预填工作项，确认后批量创建成功，建议记录为 accepted。
2. 需求中指定人选时，对应工作项主执行人预填该成员；技能明显不匹配时有提示但不阻止。
3. 拆解建议被忽略时不产生任何工作项。
4. Agent 不产生任何业务表写入（护栏测试通过）。
5. Ollama/LLM 不可用时，手动创建工作项流程不受影响。

## 8. 备注

第 3 节的全部决策已定稿（问题 1 由用户确认，其余按推荐默认值采纳）。如后续要调整"任务依赖"或"直接建草稿"两项，需另起变更，因为涉及数据表结构与护栏改动。
