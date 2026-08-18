# 多项目改造 spec

Status: ready-for-agent

> 日期：2026-08-11
> 来源：grill-with-docs 全量决策 + 子 agent 完整性核对 + 测试接缝设计（见 `docs/2026-08-11-multiproject-testing-seams.md`）
> 关联：用户接手 AgentOS，单项目 → 多项目

## Problem Statement

AgentOS 目前是**单项目**架构：登录后直接进入唯一项目的工作台，每个用户只有一份成员记录（一个角色）。用户朋友要用它服务**多个项目**——一个小团队可能同时推进多条业务线（不同客户/产品/交付对象），同一用户在不同项目里承担的角色可能不同（在 A 项目是负责人、在 B 项目是普通成员）。

当前结构无法表达：
- 数据属于哪个项目（任务/交付物/通知/文件没有项目归属）
- 同一用户跨项目的不同角色（只有一个 member 记录）
- 跨项目隔离（没有任何边界防止串数据）
- 系统管理员作为独立身份（现在 admin 是项目内的一个角色，被绑定在某个项目里）

没有项目选择入口，也没有项目级权限边界。

## Solution

从单项目扩展为多项目，核心四块：

1. **数据层**：给"有独立列表入口"的表冗余 `project_id`；派生表通过父对象推导归属。三条铁律：唯一事实来源、应用层规则（service 层填充，API 不接受输入，跨实体引用同项目校验）、越权即 404。
2. **身份/权限**：引入 `X-Project-Id` 请求头作为项目上下文（缺失 400、非成员 403）；admin 升级为**全局角色**（`users.is_admin`，不属于任何项目，进管理控制台）；项目创建仅 admin。
3. **接口层**：所有项目内查询按 project_id 过滤；对象级越权返回 404；`GET /me/projects` 列出我参与的项目；幂等键并入 project_id。
4. **前端**：登录后分流（admin → 管理控制台，普通用户 → 项目选择页 → 工作台）；项目选择页记住上次 + 24h 过期；store 项目化；所有 API 请求自动带 `X-Project-Id`。

产品尚无真实用户，无存量数据兼容义务 → 迁移采用重建。

## User Stories

1. 作为普通用户，我希望登录后看到一个项目选择页面，列出我参与的所有项目，以便我进入对应的项目工作台。
2. 作为普通用户，我希望在项目列表里看到我在每个项目的角色徽章（负责人/成员），以便我知道自己在该项目的权限。
3. 作为普通用户，我希望系统记住我上次进入的项目，24 小时内再次登录直接进入，以便减少重复操作。
4. 作为普通用户，我希望 24 小时后重新看到项目选择页，以便项目发生变化时我能重新选择。
5. 作为普通用户，我在 A 项目里只能看到 A 项目的任务、交付物、通知、审批、文件，看不到 B 项目的任何数据，以便项目之间严格隔离。
6. 作为普通用户，我在 A 项目里是负责人、在 B 项目里是普通成员时，两个项目里的界面和可操作项各自按我的角色显示，以便我按各项目职责工作。
7. 作为普通用户，我在切换项目后，任务列表、审批待办、仪表盘等所有数据都自动变为新项目的内容，以便我无缝切换上下文。
8. 作为普通用户，如果我直接用某个对象的 URL 访问另一个项目的对象，应该看到"不存在"（404），而不是"无权访问"，以便不泄露项目边界外的存在性信息。
9. 作为普通用户，如果我发起请求时没有带项目上下文，应该收到明确的错误（400），以便我知道请求方式不对。
10. 作为普通用户，如果我带的项目是自己不是成员的项目，应该被拒绝（403），以便越权请求被挡住。
11. 作为普通用户，我指派任务负责人时，只能选当前项目内的成员，跨项目指派应被拒绝（400），以便避免项目边界被打破。
12. 作为普通用户，我重复提交同一幂等请求（同一用户、同一键）时，在**不同项目**下不应复用上次的响应，以便幂等键不跨项目泄漏。
13. 作为负责人，我在 A 项目管理成员时，只看到并影响 A 项目的成员，以便不影响其他项目。
14. 作为负责人，我创建的交付物、发起的审批、收到的通知都自动归属当前项目，以便项目数据自洽。
15. 作为全局管理员，我希望登录后直接进入管理控制台，而不是项目选择页，以便我管理系统层面的东西。
16. 作为全局管理员，我希望我能创建新项目并在创建时指定项目负责人，以便新项目可以上线。
17. 作为全局管理员，我希望我能查看所有项目列表和账号情况、审计记录，以便做平台管理。
18. 作为全局管理员，我不参与任何项目的业务协作（不进入项目工作台做任务/审批），以便管理员身份与业务成员身份分离。
19. 作为项目负责人（被指定者），我希望新项目创建后我能立即进入项目工作台开始管理，以便项目快速启动。
20. 作为后台 worker（扫描任务），我希望任务/审批/通知正确地归属到它们所属的项目，以便不跨项目串数据、不互相 skip。
21. 作为 AI Agent 辅助功能的用户，我希望 agent 的查询和建议也限定在当前项目内，以便不把其他项目的上下文混进来。
22. 作为使用事件流的前端，我希望 SSE 连接携带项目上下文，以便只收到当前项目的实时事件。
23. 作为系统审计，我希望审计事件记录项目归属，以便能按项目追踪谁做了什么。

## Implementation Decisions

### 数据层（D1–D6）

**D1. 冗余 project_id（6 张有独立列表入口的表）**：`work_items`、`deliverables`、`notifications`、`stored_files`、`agent_runs`（项目级扫描挂项目）、`audit_events`（快照语义，落库时捕获，不靠派生）。全部 `project_id` NOT NULL，外键指向 projects。

**D2. 经父对象推导、不加 project_id 的表**：
- 审批 4 源表：`transfer_requests`、`collaboration_requests`、`deadline_change_requests`、`dev_docs`、`reviews` → 经 `work_item_id` 推导
- `agent_suggestions` → 经 `run_id` 推导

**D3. 三条铁律（应用层规则）**：
1. **唯一事实来源**：要么自身是独立入口（冗余 project_id），要么是父对象的投影（经 FK 推导）。不许出现"既能自身又有父对象"的重复归属源。
2. **应用层规则**：`project_id` NOT NULL；service 层从请求的项目上下文/父对象**派生填充**，**API 不接受传入**；跨实体引用（如把 A 项目成员指派为 B 项目任务的 assignee）做**同项目校验**，违反返回 400。
3. **越权 404**：项目边界是一堵墙——墙外对象等同于不存在。用 A 项目上下文访问 B 项目对象 → 404（不是 403）。

**D4. 现有唯一约束全不动**：`work_item_id`/`member_id` 等现有唯一键已隐含"项目内唯一"，多项目后语义不变。

**D5. 迁移策略**：产品无真实用户，采用**重建**（迁移重建，无存量兼容义务）。历史数据回填按各自表处理（audit 回填、stored_files 可空需降级规则）。

**D6. 幂等键并入 project_id**：`idempotency_records` 唯一键 `(user_id, key, method, path)` 不含项目 → 同一用户跨项目同幂等键会错误复用响应。**唯一键必须并入 project_id**。

### 身份/权限

- **admin 全局化**：`users` 表加 `is_admin` 布尔字段。admin 不属于任何项目、不参与业务，登录进 `/console` 管理控制台（项目列表/账号管理/审计）。`member_to_out` 的 role 只剩 `leader`/`member`。
- **新依赖 `get_current_admin`**：校验 `users.is_admin`，供管理接口使用。
- **`get_current_member` 改读 `X-Project-Id` 请求头**：缺失 → 400；带的项目自己不是成员 → 403。不再用 `get_default_project`。
- **项目创建仅 admin 可做**：`POST /projects`，创建时指定 leader；leader/member 不能建。
- **`/config` 端点**改用它不依赖 member 的身份解析（`get_current_user`），否则 admin 全局化后会 403。

### 接口

- 新接口 **`GET /me/projects`**：列出当前用户参与的项目（用 `get_current_user`，不用 member 依赖，避免鸡生蛋）。
- 所有项目内查询路径按 project_id 过滤；对象级访问带项目校验。

### Agent / Worker 层（子 agent 核对发现的遗漏，必须纳入）

- **`agents/graphs/base.py` 的 `load_context`** 复刻了第 7 处单项目查询（select Project limit 1），必须改为按当前项目上下文取项目。
- **`agents/tools.py`** 全部工具目前全局查询、无项目过滤（跨项目泄漏风险）——所有查询按当前项目限定。
- **`agents/service.py` 与任务队列载荷**：worker 进程**没有请求头上下文**，不能靠 header 推导项目——项目上下文必须**显式携带在队列载荷里**。
- **worker 写路径**（`workers/due_scan.py`、`risk_scan.py`）：due_scan 全表扫描、`notify()` 无 project 参数；risk_scan 项目级分析无项目维度、去重键全局化会互相 skip。**必须从 `work_item_id`/`run_id` 推导 project_id 显式传入**。这是最大坑。
- **SSE 事件载荷**：事件需带 project_id；前端 `events.ts` 缓存失效全局化（queryKey 需并入项目）、EventSource URL 需加 project_id（沿用 token 传 query 参数的方式，EventSource 不能自定义 header）。

### 前端

- **组件约定（仓库主规定）**：前端一律用 **shadcn/ui** 组件库；`dialog`、`drawer`、`alert` 三种覆盖层组件**绝对不要混用**——同一类交互只选一种，保持交互一致性。新增交互组件时沿用 `src/components/ui/` 的既有 shadcn 模式。
- **登录分流**：admin（`user.is_admin`）→ `/console` 管理控制台；普通用户 → 项目选择页 → 工作台。
- **项目选择页**：`localStorage` 存 `{project_id, last_seen_at}`，24h 内直接进上次项目，超时/首次进选择页；列出"我参与的项目"（`GET /me/projects`）带角色徽章。
- **store 项目化**：`useAuthStore` 加 `projectId`/`projects`；`member` 变为"当前项目成员"。现有 `useIsLeader`/`useIsAdmin`/`useCanManageMembers` 等角色控制自动适配当前项目。
- **admin 角色来源**：`user.is_admin`（不再读 `member.role === 'admin'`，前端 6+ 处需改）。
- **API 客户端**：`api.ts` 三个入口（api/upload/download）统一加 `X-Project-Id` header。
- **登录时序**：多项目后 `/members` 需 X-Project-Id，admin 不能调 `/members` → **必须先选项目再 `loadIdentity`**（先 `GET /me/projects` 判断分流，进项目后再加载 member 身份）。

### 已知调用点（改造时逐处覆盖）

- `get_default_project` 有 6 处调用点：`get_current_member`、`work_items/service.py`、`transfers/service.py`、`deadlines/service.py`（三处同构"通知负责人"辅助）、`project/service.py`（创建成员）、`notifications/stream.py`（SSE）+ agents/graphs 第 7 处。多数可被 `actor.project_id` 替代（好消息，改动小）。
- `ensure_default_project()`（bootstrap.py）保留为项目创建功能的地基；`ensure_admin_membership` 重写为 `users.is_admin`。

## Testing Decisions

**测试接缝（最小化、最高层）：两条，均复用既有接缝，不新建层级。**

- **主接缝 = 后端 HTTP API 层**（pytest + httpx，现有 `backend/tests/conftest.py` 扩展）：多项目核心行为是"跨项目隔离"，是系统最外层行为，只有完整 HTTP 请求能真实验证全链路（header 携带 → 依赖解析 → service 过滤 → 越权响应）。不在 service 层建独立测试层、不测状态机（与项目无关）。
- **副接缝 = 前端 Vitest 流程测试**（现有 `frontend/src/__tests__/` 模式）：UI 行为（登录分流、选择页、切换项目、角色显隐）无法用后端接缝验证。

**conftest 扩展**：`project` fixture 参数化出 A/B 两项目；`add_member` 支持跨项目建成员（同一用户既 A 项目 leader 又 B 项目 member）；`auth_headers` 同时带 `Authorization` + `X-Project-Id`。

**主接缝行为矩阵**（每条都要断言）：

| 行为 | 断言 |
|---|---|
| 隔离 | A 项目请求看不到 B 项目任务/交付物/通知/文件 |
| 对象越权 | A header 访问 B 对象 → 404 |
| header 缺失 | 不带头 → 400 |
| header 越权 | 带自己不是成员的项目 → 403 |
| 跨实体引用 | A 成员指派为 B 任务 assignee → 400 |
| 幂等键 | 同用户同键跨 A/B 项目 → 不复用响应 |
| admin 全局化 | admin 无成员记录仍可访问管理接口；审计接口对 admin 放行 |
| worker 写路径 | due_scan/risk_scan 从 work_item/run 推导 project_id 正确落库 |
| 项目级派生 | deliverables/审批项经 work_item 推导正确；agent_suggestions 经 run 推导正确 |

**副接缝行为矩阵**：登录分流（admin → console；普通 → 选择页）、24h 记忆（有效直接进 / 超时进选择页）、选择页列出我参与的项目 + 角色徽章、store 项目化后 `useIsLeader` 跟随当前项目、API 客户端自动带 header / SSE URL 带 project_id、admin 角色来源 `user.is_admin`。

**边界（明确不测）**：不测 Provider/模型调用细节（无项目上下文）；不测幂等键底层存储，只测跨项目行为；不测 24h 时间流逝本身（注入时间/直接断言分支）；前端沿用 mock API 模式，不测真实后端联调。

**Prior Art**：`backend/tests/test_transfers_api.py`、`test_reviews_api.py`、`test_members.py`、`test_agent_suggestions_api.py` + `conftest.py` fixtures；前端 `__tests__/member-flow.test.tsx`、`leader-flow.test.tsx`、`admin-role.test.tsx`、`e2e/full-workflow.test.tsx`。

**实现顺序依赖**（测试接缝决定）：数据层（6 表 project_id + conftest 双项目）→ 身份/权限（X-Project-Id、admin 全局化）→ 接口层（过滤/归属/幂等）→ 前端（store/分流/选择页）。每步完成即跑主接缝回归。

## Out of Scope

- **不做**：项目内更细粒度的数据权限（如仅某成员可见的任务、部门隔离）——本次只有"项目"这一个隔离维度。
- **不做**：多项目仪表盘的聚合视图（跨项目汇总报表）——需要时后续单独 spec。
- **不做**：项目归档/删除、项目设置页的完整管理界面——管理控制台本期只覆盖项目创建、账号、审计的基线。
- **不做**：邀请链接/自助加入项目——成员关系本期由 admin 建项目 + 负责人管理。
- **不做**：存量数据迁移的兼容层——产品无真实用户，重建即可，不做灰度/双写。

## Further Notes

- **风险清单**（实现时逐一核对）：NOT NULL project_id 与历史数据（audit 回填、stored_files 可空降级）；约 40 个端点依赖 `get_current_member`；worker 写路径（必须显式携带 project_id）；`get_current_leader_or_admin` 依赖 `get_current_member`（admin 全局化后审计接口会挂）；`require_admin_for_admin_target`/`list_members` 的 admin 分支成死代码需清理；前端登录时序（先选项目再加载 member）。
- **协作规则**：仓库主规定禁止直接推 main，全走 `feat/` 分支（本 spec 已在 `feat/multiproject` 分支上）。实现按 ticket 逐条做，每条独立提交。
- 测试接缝文档：`docs/2026-08-11-multiproject-testing-seams.md`（独立成文，本 spec 的 Testing Decisions 是其摘要）。
