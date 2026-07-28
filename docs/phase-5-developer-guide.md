# 阶段 5 开发者指南：多 Agent 辅助

本文面向刚加入 AgentOS 的开发者，说明阶段 5（T5.1–T5.7）实现的**实际形态与设计理由**。文中章节号指设计文档 `docs/2026-07-26-agentos-workflow-platform-design.md`。阶段 1–4 的基线与机制（错误格式、幂等、审计、认证、工作项、协作、SSE、存储抽象、shadcn/ui 约定）见 `docs/phase-1/2/3/4-developer-guide.md`，本文不重复。

阶段 5 交付内容：

- `ModelProvider` 适配层：Ollama 默认 + OpenAI 兼容，httpx 实现，统一错误封装（T5.1，第 15 章）。
- `agent_runs`/`agent_suggestions` 表与 LangGraph 基础图，检查点持久化到 PostgreSQL（T5.2，10.2、17.3 节）。
- 统一 Pydantic 输出 Schema、结构校验诊断与工具注册表权限护栏（T5.3，10.2、10.3 节）。
- 六个辅助 Agent：需求/分配/规划（T5.4）与风险/初审/总结（T5.5），10.1 节。
- 失败恢复：ZSET 延迟队列指数退避重试 + 人工重新触发（T5.6，17.3 节）。
- 建议查询/反馈 API、前端建议中心与需求引导创建工作项（T5.7，12.5、13.1 节）。

验证基线：`docker compose exec backend pytest` → **261 passed**；`npm run build`（tsc strict）零错误；六服务全部 healthy。

核心原则贯穿全阶段：**人类决定，Agent 建议**（原则 2）——Agent 只生成 `agent_suggestions` 记录，不具备任何写业务状态的工具；人工确认后由前端/用户调用正式业务命令。

## 1. ModelProvider 适配层（T5.1，第 15 章）

代码位置：`backend/app/infrastructure/models/`（与 storage/queue 同级的技术机制，业务代码不得直接实例化具体模型客户端）。

- `provider.py`：`ModelProvider` ABC——`name`/`model`/`is_external` 属性 + `generate(prompt, *, system=None, json_output=False) -> str`。`get_model_provider()` 单例工厂（仿 `get_storage_provider()`），`reset_model_provider()` 供测试。验收 grep：`backend/app` 内无对具体 Provider 或 `httpx.AsyncClient` 的直接实例化（全部封装在本目录内）。
- `ollama.py`（默认）：POST `{OLLAMA_BASE_URL}/api/chat`，system+user 消息，`format=json` 支持 `json_output=True`；超时/连接失败按 `LLM_MAX_RETRIES` 线性退避重试（瞬时抖动），非 2xx 直接抛错不重试。`is_external=False`。
- `openai_compatible.py`：POST `{BASE_URL}/chat/completions`，Bearer key，`response_format=json_object`；`is_external=True`。
- `errors.py`：`ModelError`/`ModelUnavailableError`/`ModelTimeoutError`——httpx 异常一律封装，不外漏（17.3 节上层按统一类型处理）。
- 配置（compose 三服务 backend/worker/scheduler 与 `.env.example` 已接）：`LLM_PROVIDER=ollama`、`LLM_MODEL`、`OLLAMA_BASE_URL=http://host.docker.internal:11434`、`OPENAI_COMPATIBLE_BASE_URL=`、`OPENAI_COMPATIBLE_API_KEY=`、`LLM_TIMEOUT_SECONDS=60`、`LLM_MAX_RETRIES=2`。scheduler 补了 `extra_hosts: host.docker.internal:host-gateway`（backend/worker 阶段 1 已有）。`settings.llm_is_external` property 供 T5.7 前端提示。
- **不引入 langchain**：两个 Provider 均用 httpx 直连（httpx 从 dev 依赖提升为主依赖），满足"不与 Ollama 绑定"且保持依赖最小。
- 测试 `tests/test_model_provider.py`（6 个）：全部 `httpx.MockTransport`——Ollama 成功调用（路径/消息/format 断言）、工厂切换 OpenAI 兼容零代码改动、连接失败重试后抛 `ModelUnavailableError`、超时抛 `ModelTimeoutError`、非 2xx 不重试、外部标识。不依赖真实 Ollama。

## 2. 运行记录表与 LangGraph 基础图（T5.2，10.2、17.3 节）

### 2.1 表结构（迁移 0009）

`backend/app/agents/models.py`：

- `agent_runs`：id / status（`pending|running|succeeded|failed`，CHECK）/ agent_type / model / trigger_source（`manual|scheduler|event`）/ work_item_id（FK **可空**，项目级运行为 NULL）/ duration_ms / error / retry_count / request_id / 时间戳。迁移 0010 追加 `prompt` 列（人工重试需原样重投，见 6.3 节）。
- `agent_suggestions`：id / run_id(FK) / suggestion_type / content JSONB / confidence / risks / fact_refs JSONB / review_status（`pending|accepted|ignored`）/ reviewed_by / reviewed_at / prompt_version / 时间戳。

### 2.2 基础图

`backend/app/agents/graphs/base.py`：StateGraph 五节点 `load_context → route_capability → run_capability → validate_output → save_suggestion`（对应 10.2 节流程图）：

- `AGENT_ROUTES`/`CAPABILITIES` 注册表：agent_type → 能力函数（签名 `(state) -> suggestion dict`，支持 async）。占位 `echo` 能力（不调模型的健康探针，`echo.v1`）用于全链路自检。
- `AgentGraphState` 全 JSON 可序列化：run_id、agent_type、trigger_source、work_item_id、request_id、prompt、context、capability、suggestion 等。
- `build_agent_graph(checkpointer)` 由调用方注入检查点；`AsyncPostgresSaver`（langgraph-checkpoint-postgres + psycopg）持久化到 PostgreSQL，DSN 由 DATABASE_URL 去 `+asyncpg` 转换，`setup()` 幂等建检查点表，**thread_id = run_id**（重试/恢复天然以 run 为单位，不产生重复建议）。
- 检查点不替代业务记录（原则 1）：业务状态仍以业务表为准。

### 2.3 触发与执行链路

- `app/agents/service.py` 的 `request_agent_analysis()` 是唯一触发入口：建 run(pending) → Redis 队列投递 `agent.run`（复用 T1.6 队列）。
- `app/workers/agent_run.py` 的 `execute_agent_run()`：running → 注入 checkpointer 执行图 → succeeded/failed + duration_ms + error；成功后发 SSE 事件 `agent.suggestion_ready`。
- `save_suggestion` 节点：保存建议与通知同事务，接收人 = 项目 leader（复用 T3.5 notify）。
- 依赖新增：`langgraph==1.2.9`、`langgraph-checkpoint-postgres==3.1.0`、`psycopg[binary,pool]==3.3.4`（主库访问仍是 asyncpg，psycopg 仅供检查点，两者并存）。
- 测试 `tests/test_agent_runs.py`（3 个）：建表、端到端（投递→handle_task→succeeded+检查点行+建议+通知）、图内抛错→failed 无建议无通知。真实栈实测：echo 分析 run succeeded（1560ms）、checkpoints 7 行、通知 1 条。

## 3. 输出 Schema 与权限护栏（T5.3，10.2、10.3 节）

### 3.1 统一输出 Schema

`backend/app/agents/schemas/suggestion.py`：

- `AgentSuggestionOutput`（模型输出部分）：`suggestion_type`、`content`（`summary`+`rationale` 必填，`extra="allow"` 允许各能力平铺自有字段）、`fact_refs: dict[str, list[str]]`、`confidence ∈ [0,1]`、`risks`、`prompt_version`。
- `AgentSuggestionEnvelope`：系统侧填充 `run_id`/`model`（model 以 `agent_runs.model` 为准不冗余入库）。
- 校验入口 `parse_suggestion_output(raw, run_id=...)`：接受 dict 或 JSON 字符串（先 `json.loads`）。
- prompt_version 约定：`<agent_name>.v<N>`，由能力函数随输出声明。

### 3.2 结构校验与诊断（17.3 节）

`validate_output` 节点做 Schema 严格校验。失败抛 `SuggestionValidationError`，诊断 JSON（`{"run_id", "stage": "json_parse"|"schema_validate", "errors", "raw_output"截断500字符}`）写入 `agent_runs.error`，run 标记 `failed`，**不落 agent_suggestions、不发通知**。诊断用既有 `failed` 状态，未新增状态、未加迁移。

### 3.3 工具注册表（10.3 节）

`backend/app/agents/tools.py` 的 `TOOL_REGISTRY: dict[str, AgentTool]`，`kind ∈ {"read_query", "write_suggestion"}`：

- read_query（11 个）：`get_work_item_overview`、`list_open_work_items`、`list_member_capabilities`、`get_member_workload`、`list_deliverable_metadata`（text/git_link 带 content，file 只给元数据子对象——**不读文件原文**，16 节最小上下文）、`list_blocked_items`、`list_transfer_history`、`list_waiting_collaborations`、`get_work_item_status_counts`、`list_recently_completed_work_items`、`list_pending_approvals`。
- write_suggestion 唯一写工具：`write_suggestion()`，只写 `agent_suggestions`。
- `FORBIDDEN_OPERATIONS` 结构化列出 10.3 节七项禁止操作（create_work_item / change_assignee / approve_transfer / change_deadline / approve_review / delete_file_or_record / merge_code），不注册为工具。
- 模块只 import domain 的 `models`/只读常量，**不 import 任何 domain service**。

护栏测试 `tests/test_agent_guardrails.py`（10 个）：遍历注册表断言无写业务命令 + 源码级断言（tools.py 无 service import、read 工具无 `session.add/delete`）；非法 JSON/缺字段全链路 → 诊断落库、零建议、零通知；成功 run 后断言 `audit_events` 无业务前缀（`work_item.`/`transfer.`/`review.` 等）事件——支撑第 22 章标准 10。

## 4. 六个辅助 Agent（T5.4/T5.5，10.1 节）

代码位置：`backend/app/agents/specialists/`（每 Agent 一个模块）+ `backend/app/agents/prompts/`（提示词模板，SYSTEM_PROMPT 声明"只输出 JSON"，`render_user_prompt()` 组装最小上下文）。公共助手 `specialists/common.py`：`call_model_json()`（经 `get_model_provider()`，json_output=True，模型错误原样冒泡给 run 层）与 `build_output()`（解析模型 JSON，**系统侧注入权威字段** suggestion_type/prompt_version/fact_refs——不信任模型自报；非法 JSON 透传给 validate_output 走诊断路径）。

| agent_type | suggestion_type | prompt_version | content 平铺字段 | 触发方式 |
|---|---|---|---|---|
| `requirement_analyst` | `requirement` | `requirement_analyst.v1` | goals/constraints/deliverables/acceptance_criteria | 人工（API） |
| `assignment_advisor` | `assignment` | `assignment_advisor.v1` | recommended_assignee/candidates[]/capability_adjustments[]（仅建议，不自动改能力，6.2 节） | 人工 |
| `planning_advisor` | `planning` | `planning_advisor.v1` | work_item_breakdown[]/collaboration_points[] | 人工 |
| `workflow_risk` | `risk` | `workflow_risk.v1` | risks[{type: overdue\|blocked\|frequent_transfer\|collaboration_wait, severity, detail, ...}] | **scheduler 周期** + 人工 |
| `deliverable_review` | `review` | `deliverable_review.v1` | checklist[{checkpoint, verdict: pass\|fail\|uncertain, evidence}] | **submit 事件** |
| `summary_agent` | `summary` | `summary_agent.v1` | progress/completed[]/pending_approvals[]/risks[] | 人工（项目级） |

fact_refs 由系统侧查询注入：assignment 引用真实 `member_ids`（能力+负载）与 `work_item_ids`；risk 引用 work_item/collaboration/transfer ids；review 引用 deliverable_ids；summary 引用完成/待审统计。

### 4.1 触发链路细节

- **人工触发（工作项级）**：`POST /api/v1/work-items/{id}/agent-analysis`，入参 `{agent_type, prompt?}`，202 返回 `AgentRunOut`。权限：leader 或工作项相关成员（复用 `files.service.is_work_item_related`）；未注册 agent_type → 400。
- **人工触发（项目级）**：`POST /api/v1/agent-analysis`，仅 leader，work_item_id 可空（项目级风险扫描、Summary 用）。
- **scheduler 周期风险扫描**（4.2 节）：`workers/risk_scan.py` + scheduler 第三个周期任务 `agent.risk_scan`，间隔 `AGENT_RISK_SCAN_INTERVAL_SECONDS`（默认 3600）。去重：存在 pending/running 的 workflow_risk run 则跳过本轮。
- **submit 事件触发初审**：`work_items/service.py` 的 `run_command("submit")` 在业务 commit + 事件发布**之后**调 `_dispatch_deliverable_review()` 投递 `agent.run`（trigger_source="event"）；try/except 尽力而为，投递失败只记日志不影响 submit（有专门测试）。
- 初审最小上下文：验收标准 + 文本交付物内容 + 文件元数据（不读原文）+ Git 链接文本；初审结果只进 agent_suggestions 并通知负责人，**`reviews` 表无 Agent 写入**（有测试断言）。
- Summary Agent 仅人工触发，未做周期任务（日报低频，避免持续占用模型调用；如需周期化照 `agent.risk_scan` 范本挂第四个任务）。

测试：`test_agent_specialists.py`（4）+ `test_agent_analysis_api.py`（6）+ `test_agent_t55.py`（6），全部 mock 模型，覆盖 Schema 契约、真实能力/负载数据引用、submit 触发+reviews 零写入、投递失败不拖垮 submit、摘要统计一致性、权限矩阵。

## 5. 失败恢复（T5.6，17.3 节）

### 5.1 指数退避重试

- 实现：`infrastructure/queue/queue.py` 新增 **ZSET 延迟队列**（`enqueue_delayed()`/`promote_due_delayed()`），与既有 List 队列同一套机制，无新组件。失败任务 ZADD（score = now + delay），worker 主循环每轮 BRPOP 前把到点任务搬回即时队列。
- 间隔：`delay = AGENT_RUN_RETRY_BASE_SECONDS * 2^attempt`（默认 base=30s，`AGENT_RUN_MAX_RETRIES=3` → 30/60/120，第 4 次失败终态 failed）。配置已同步 compose（backend/worker）与 `.env.example`。
- **两层重试关系**：provider 层 `LLM_MAX_RETRIES` 是单次调用内线性重试（瞬时抖动）；run 层指数退避是第二道，provider 耗尽后错误才冒泡，不叠加放大。
- **错误分类**（`workers/agent_run.py` 的 `is_retryable_error()`）：模型超时/不可用及图执行异常可重试；`SuggestionValidationError`（Schema 校验失败）是确定性错误**不重试**，直接 failed。
- 幂等：重投沿用原 payload、同一 run_id（thread_id 不变），重试期间 run 回 `pending`、`retry_count+1`、error 留档。

### 5.2 人工重新触发

`POST /api/v1/agent-runs/{id}/retry`：仅 `failed` 可重试（其余 409 `AGENT_RUN_NOT_FAILED`）；权限 leader 或 run 关联工作项相关成员（项目级 run 仅 leader）。语义：status→pending、error/duration 清空、**retry_count 清零**（人工重触发即承认自动退避已耗尽，给新一轮完整预算），按 run 持久化的原 agent_type/work_item_id/prompt 重投，202 返回 `AgentRunOut`。前端"重新触发"按钮只对 failed 展示，409 表示状态已变化需刷新。

### 5.3 核心流程不受影响（第 22 章标准 9）

worker 新增 `safe_handle_task` 包裹单任务异常——Agent run 失败或处理器意外异常均不拖垮后续任务消费。有测试：模型持续不可用期间登录、建工作项正常，worker 继续处理其他类型任务。

### 5.4 DDL 影响分析降级（8.4 节）

**保持现状未改动**：T3.4 的 DDL 影响分析是同步规则化分析（非 Agent），`deadlines/service.py` 已 try/except 降级为 `impact_analysis_status=unavailable` 并照常推进 PENDING_APPROVAL，已有测试覆盖。降级语义完整，接线 Agent 建议属过度建设。

## 6. 建议 API、反馈与前端建议中心（T5.7，12.5、13.1 节）

### 6.1 后端端点（`backend/app/agents/router.py`）

| 端点 | 说明 | 权限 |
|---|---|---|
| `GET /agent-suggestions` | 过滤：suggestion_type/review_status/work_item_id + limit/offset；返回含 join run 的 work_item_id/model | 登录成员可读 |
| `POST /agent-suggestions/{id}/feedback` | `{action: "accepted"\|"ignored"}`；写 review_status/reviewed_by/reviewed_at；重复反馈 409 `AGENT_SUGGESTION_ALREADY_REVIEWED` | 仅 leader |
| `GET /agent-runs` / `GET /agent-runs/{id}` | 运行记录列表/详情（error/duration_ms/retry_count），前端引导流程轮询用 | 登录成员可读 |
| `GET /config` | `{llm_provider, llm_is_external}`，只暴露非敏感标识 | 登录成员 |

反馈写审计 `agent.suggestion_feedback`（`agent.` 前缀，不触碰护栏断言的业务前缀清单）。建议查询全员可读、反馈仅 leader——建议本身无敏感信息，与团队透明语义一致（取舍说明见端点 docstring）。

测试 `test_agent_suggestions_api.py`（14 个）：过滤、feedback 权限/409/422、审计可查。

### 6.2 前端 `features/agent-assistant/`

- `AgentAssistantPage.tsx`（路由 `/agent-assistant`，建议中心）：类型/状态过滤、建议卡片（类型 Badge、置信度、时间、关联工作项链接，项目级显示"项目级建议"）、展开详情（summary/rationale/结构化内容/风险限制/fact_refs/模型/prompt_version/run_id/反馈时间）、采纳/忽略按钮（仅 leader 且 pending）、运行记录表（failed 行"重新触发"调 retry API）。
- `SuggestionContent.tsx`：六类 content 结构化渲染（risks 严重度 Badge、checklist verdict Badge、candidates 列表、work_item_breakdown 卡片等），未识别类型回退 JSON。
- `RequirementGuidedCreateDialog.tsx`（创建工作项引导，13.1 节）：自然语言输入 → `POST /agent-analysis`（requirement_analyst）→ 轮询 `GET /agent-runs/{id}`（2s，终态停止）→ 展示建议 → 预填**可编辑**表单 → 确认后前端调既有 `POST /work-items` 并 best-effort 写 accepted 反馈；**忽略则只写 ignored 反馈，无任何业务写入**（原则 2：业务写入始终由人发起）。入口在工作项页（"AI 需求引导"）与建议中心。
- 外部数据提示（16 节）：`GET /config` 的 `llm_is_external=true` 时，建议中心顶部与引导对话框内显示琥珀色警示"数据将发送至外部服务"（沿用既有 `bg-amber-50` 风格，未新增 shadcn 组件）。
- SSE：`services/events.ts` 监听 `agent.suggestion_ready` → 失效 `["agent-suggestions"]`/`["agent-runs"]` query + toast；建议中心自动刷新。worker 侧事件已在 T5.2 接好，curl 长连接实测收到帧。
- `npm run build`（tsc strict + noUnusedLocals）零错误。

### 6.3 真实栈冒烟

全量重建后 curl 实测：echo 分析 run succeeded → 建议过滤查询命中 → leader feedback accepted 落库 → 重复反馈 409 → `GET /config` 正确 → SSE 收到 `agent.suggestion_ready` 帧 → `agent.suggestion_feedback` 审计事件可查。

## 7. 测试与配置汇总

- 全部测试 `docker compose exec backend pytest` → **261 passed**（阶段 4 的 205 个无回归）。新增 56 个：model provider 6、agent runs 3、护栏 10、specialists 4、analysis API 6、T5.5 六个 Agent 6、retry 7、suggestions API 14。
- 迁移序列追加：`0009_agent_runs_suggestions` → `0010_agent_runs_prompt`（agent_runs.prompt 列）。
- 新增配置：`LLM_PROVIDER`、`LLM_MODEL`、`OLLAMA_BASE_URL`、`OPENAI_COMPATIBLE_BASE_URL`、`OPENAI_COMPATIBLE_API_KEY`、`LLM_TIMEOUT_SECONDS`、`LLM_MAX_RETRIES`、`AGENT_RISK_SCAN_INTERVAL_SECONDS`、`AGENT_RUN_MAX_RETRIES`、`AGENT_RUN_RETRY_BASE_SECONDS`（compose/.env.example 已接）。
- 新增依赖：httpx（升主依赖）、langgraph、langgraph-checkpoint-postgres、psycopg[binary,pool]。
- 前端无测试框架，`npm run build` 是唯一静态检查，已通过。

## 8. 已知取舍与阶段 6 衔接

1. **宿主机无 Ollama 时全部 Agent run 干净地落 failed**（error 如 `Ollama Provider 需要配置 LLM_MODEL`），核心流程不受影响（标准 9）；真实模型链路的端到端体验需有 Ollama 的环境复验（验收场景属阶段 6 T6.3/T6.4）。echo 占位能力可无模型验证全管道。
2. 两层重试：provider 层线性（瞬时抖动）+ run 层指数退避（持续故障），语义已写进 config 与 agent_run 注释；调参先动 run 层。
3. 触发接口未加 `Idempotency-Key` 守卫（每次触发即一次新 run，语义合理）；审批类业务命令的幂等约定不变。
4. 通知接收人目前一律为项目 leader（10.2 节"通知相关人员"的最简实现）；若需按工作项相关成员分发，改 `base.py` 的 `save_suggestion` 节点即可。
5. feedback 无 comment 字段（表结构未提供，保持最小改动）；如需文字反馈加列即可。
6. Summary Agent 仅人工触发；周期化照 `agent.risk_scan` 范本。
7. 主库可能留有开发自检产生的 echo run/suggestion/notification 数据，可按需清理。
8. 阶段 6 衔接点：Agent 合约测试（结构化输出/超时/解析失败）已有 `test_agent_guardrails.py`/`test_model_provider.py` 基础，T6.1 可直接扩展；标准 9/10 的验证测试已就位（worker 韧性、护栏）；安全检查清单（T6.6）注意 `GET /config` 只暴露非敏感标识、日志不记录 API Key。
