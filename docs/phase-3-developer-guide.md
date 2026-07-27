# 阶段 3 开发者指南：动态协作

本文面向刚加入 AgentOS 的开发者，说明阶段 3（T3.1–T3.7）实现的**实际形态与设计理由**。文中章节号指设计文档 `docs/2026-07-26-agentos-workflow-platform-design.md`。阶段 1/2 的基线与机制（错误格式、幂等、审计、认证、工作项、shadcn/ui 约定）见 `docs/phase-1-developer-guide.md` 与 `docs/phase-2-developer-guide.md`，本文不重复。

阶段 3 交付内容：

- 协作请求：数据模型、8.2 节状态机、发起与处理 API（T3.1/T3.2，7.2、12.4 节）。
- 主任务转派申请与负责人审批（T3.3，7.3、8.3 节）。
- DDL 变更申请与影响审批，区分协作级与主任务级（T3.4，7.4、8.4 节）。
- 站内通知与 `GET /approvals` 审批聚合（T3.5，12.6 节）。
- SSE 实时事件流 `GET /events/stream` 与到期/逾期提醒扫描（T3.6，4.3 节）。
- 前端协作区、审批中心、待办中心、通知、时间线与 SSE 自动刷新（T3.7，13.1/13.2 节）。

验证基线：`docker compose exec backend pytest` → **172 passed**；六服务全部 healthy。

## 1. 协作请求（T3.1/T3.2，7.2、8.2 节）

代码位置：`backend/app/domains/collaboration/`（state_machine/models/schemas/service/router 五件套，与 work_items 同构）。

### 1.1 数据模型（迁移 0005）

`collaboration_requests`：work_item_id、requester_id（发起人）、assignee_id（接收人）、title、goal（独立目标）、template（模板，可空）、due_at（协作 DDL，可空）、result_text（回传产物文本，可空）、status、version（乐观锁）。

### 1.2 状态机（8.2 节）

纯函数 `transition()`，9 条合法迁移：

```text
REQUESTED --accept--> ACCEPTED --start--> IN_PROGRESS --submit--> SUBMITTED
REQUESTED --decline--> DECLINED
SUBMITTED --request_revision--> REVISION_REQUESTED --start--> IN_PROGRESS
SUBMITTED --complete--> COMPLETED
REQUESTED/ACCEPTED --cancel--> CANCELLED
```

取舍说明（与 8.2 原文的差异）：

- `ACCEPTED→CANCELLED` 原文为"双方确认取消"，首版简化为发起人或接收人单方即可取消。
- `REVISION_REQUESTED→IN_PROGRESS` 复用 `start` 命令与 `/start` 端点，不新增 resume 端点。
- `/start` 是 12.4 节清单之外补充的端点（状态机必需）。

### 1.3 业务规则与接口

- 发起：`POST /work-items/{id}/collaboration-requests`，**仅工作项当前主执行人**，无需负责人审批（2.1 节）；接收人不能是自己、必须是项目活跃成员；发起后接收人自动加入 `work_item_collaborators`（同事务）。
- 权限：accept/decline/start/submit 仅接收人；request-revision/complete 仅发起人；cancel 双方均可。submit 携带 `result_text`（文件类产物阶段 4 接入）。
- 硬规则（7.2 节）：协作请求任何状态变化**不触碰** `work_items.assignee_id` 与工作项状态，有测试断言。
- 查询：`GET /work-items/{id}/collaboration-requests`（工作项维度）、`GET /collaboration-requests?role=sent|received`（我的协作）、`GET /collaboration-requests/{id}`（完整详情，含 goal/template/result_text）。
- 全部命令支持 `Idempotency-Key` + version；冲突 409 `COLLABORATION_VERSION_CONFLICT`；非法迁移 409 `COLLABORATION_INVALID_TRANSITION`。

## 2. 转派申请与审批（T3.3，7.3、8.3 节）

代码位置：`backend/app/domains/transfers/`。

- `transfer_requests`（迁移 0006）：work_item_id、from_member_id、to_member_id、reason、impact_note、status（PENDING/APPROVED/REJECTED/CANCELLED）、approved_by/at、version；`agent_suggestion_id` 可空列**预留给阶段 5** Agent 能力匹配建议。
- 状态机：`PENDING → APPROVED | REJECTED | CANCELLED`（发起人可取消）。
- 唯一待审批约束（8.3、17.2 节）：**双层方案**——应用层查重给友好 409 `TRANSFER_PENDING_CONFLICT`（details 带 pending_request_id）+ PostgreSQL 唯一部分索引 `uq_transfer_requests_pending_per_item`（`work_item_id WHERE status='PENDING'`）兜底并发窗口，service 捕获 IntegrityError 转同一 409。单表插入无法靠乐观锁防并发双写，部分索引是最简且正确性最强的保证。
- 审批通过才在一个事务中：更新 `work_items.assignee_id`（version+1）+ 更新申请状态 + 审计（`work_item.assignee_changed`，before/after 含 assignee_id 与 transfer_request_id，支撑第 22 章标准 2"历史负责人完整追溯"）+ 通知新旧负责人。审批前负责人不变。并发重复审批（相同/不同幂等键）只生效一次。
- 接口：`POST /work-items/{id}/transfer-requests`（仅当前主执行人）、`POST /transfer-requests/{id}/approve|reject`（负责人，可带 decision_note）、`/cancel`（发起人）、`GET /work-items/{id}/transfer-requests`、`GET /transfer-requests?role=mine`、`GET /transfer-requests/{id}`。

## 3. DDL 变更与影响审批（T3.4，7.4、8.4 节）

代码位置：`backend/app/domains/deadlines/`。

### 3.1 数据模型

`deadline_change_requests`：target_type（`work_item`|`collaboration_request`）、target_id、work_item_id（冗余便于查询与唯一约束）、old/new_due_at、reason、impact_analysis（JSONB 可空）、impact_analysis_status（`generated`|`unavailable`）、status、requested_by、approved_by/at、version。

### 3.2 状态机与两级审批规则（7.4 节）

`PENDING_IMPACT_ANALYSIS → PENDING_APPROVAL → APPROVED | REJECTED | CANCELLED`。规则化影响分析是同步快操作，创建时同事务生成并直接推进到 PENDING_APPROVAL；分析异常时 `impact_analysis_status=unavailable` 照常推进——**分析失败不阻塞人工审批**（8.4 节），前端显示"未生成 AI 影响分析"。`PENDING_IMPACT_ANALYSIS` 在当前同步实现下仅事务内瞬时出现，为阶段 5 异步 AI 分析预留。

- **协作级**（目标为协作请求 DDL）：新 DDL ≤ 主任务 DDL（或主任务无 DDL）→ 双方直接确认，同事务更新 `collaboration_requests.due_at`，申请直接落 APPROVED（审计 after 含 `auto_approved: true`），无需负责人；新 DDL 晚于主任务 DDL → 走负责人审批流。
- **主任务级**：一律负责人审批；同一工作项只能有一个待审批主 DDL 变更（唯一部分索引 `uq_deadline_change_pending_main_per_item` + 应用层 409 `DEADLINE_CHANGE_PENDING_CONFLICT`）；未经批准 due_at 不变。
- 审批通过：同事务更新目标 due_at（目标表 version+1）+ 审计 + 通知。
- 规则化影响分析内容：`{target, work_item, exceeds_work_item_due, affected_collaboration_requests:[...]}`——受影响协作清单、新旧 DDL 对比、是否晚于主任务 DDL。

### 3.3 接口

`POST /work-items/{id}/deadline-change-requests`（协作级=协作双方；主任务级=主执行人或负责人）、`POST /deadline-change-requests/{id}/approve|reject`（负责人）、`/cancel`（发起人）、`GET /work-items/{id}/deadline-change-requests`、`GET /deadline-change-requests?role=mine`、`GET /deadline-change-requests/{id}`（含完整 impact_analysis）。

## 4. 通知与审批聚合（T3.5）

代码位置：`backend/app/domains/notifications/`、`backend/app/domains/approvals/`。

### 4.1 通知

- `notifications` 表（迁移 0005）：recipient_id、type（复用事件动作名）、title、body（仅摘要，16 节——审批意见等只进审计不进通知）、link（前端跳转路径）、is_read、read_at。
- 写入服务 `notify(session, recipient_id, type, title, body, link, outbox=None)`：只 flush，与业务事件同事务；传 outbox 时同步收集 SSE 事件（见第 5 节）。
- 接口：`GET /notifications?unread_only=&limit=&offset=` → `{items, unread_count}`；`POST /notifications/{id}/read`（仅本人，**幂等**，他人通知 404）。

### 4.2 通知事件清单

| 事件 type | 接收人 |
|---|---|
| `collaboration.requested` | 接收人 |
| `collaboration.accepted` / `declined` / `submitted` | 发起人 |
| `collaboration.revision_requested` / `completed` | 接收人 |
| `transfer.requested` / `deadline_change.requested` | 全体活跃负责人 |
| `transfer.approved` | 发起人 + 新负责人 |
| `transfer.rejected` / `deadline_change.rejected` | 申请人 |
| `deadline_change.approved` | 协作级自动生效→协作对端；负责人批准→申请人 |
| `reminder.due_soon` / `reminder.overdue` | 主执行人 / 协作接收人 |

work_item.* 状态变化只发 SSE 事件、不写通知行（看板可见、非待办，避免打扰未读计数）。

### 4.3 GET /approvals（12.6 节）

负责人待审批聚合：PENDING 转派 + PENDING_APPROVAL 主任务级 DDL 变更，统一形状（kind、summary、requested_by、status、impact_analysis_status、version + 各类专有字段），按 created_at 倒序。**仅负责人有数据，普通成员返回空列表（不 403）**，匿名 401。

## 5. SSE 实时事件流与到期提醒（T3.6，4.3 节）

### 5.1 事件通道层

`backend/app/infrastructure/events/`（与 queue 同级的 Redis 技术机制，生产方横跨四个领域 + worker，故不放领域内）：

- 按成员频道 `agentos:events:{member_id}` 发布——**不在客户端侧过滤他人事件**（16 节最小暴露）。
- 载荷：`{id, type, data:{title, body, link}, created_at}`。
- **publish 时机必须在 DB commit 之后**：业务 service 在 notify 处传 `outbox=events` 收集 `OutgoingEvent`，commit 成功后 `publish_after_commit(events)`（自建短连接，失败仅 warning——通知表是兜底通道）。幂等重放不执行 service，天然不重复发布。

### 5.2 SSE 端点

`GET /api/v1/events/stream`（`domains/notifications/stream.py`）：

- 认证：EventSource 无法自定义请求头，用 `?token=<access_token>`（兼容 Authorization 头便于 curl）；校验逻辑与 get_current_member 一致，认证用短会话，流式期间不持有 DB 连接。
- 帧格式：`id:` / `event:` / `data:` 三段；连接即送 `: connected`，15s 无事件发 `: ping` 心跳帧防代理超时。
- `Last-Event-ID` 仅接受不补发（pub/sub 无历史；漏发由前端"收事件即失效查询缓存"兜底）。
- nginx：`frontend/nginx.conf` 有专用 location `/api/v1/events/stream`（`proxy_buffering off`、1 小时读超时），经 3000 端口流式可达。
- SSE 只读，所有写操作走 REST；接口清单中无 WebSocket（4.3 节）。

### 5.3 到期/逾期提醒扫描

- scheduler 新增 `due.scan` 周期任务（`DUE_SCAN_INTERVAL_SECONDS`，默认 300s），与 example.ping 共用单循环双调度项（各自周期、monotonic 计时）。
- worker `app/workers/due_scan.py`：扫描未来 24 小时内到期（`DUE_SOON_HORIZON_HOURS`）与已逾期的未终态工作项/协作请求，写通知 + 发 SSE。**Worker 只写通知/事件，不触碰业务状态（4.2 硬约束）。**
- 去重：Redis 键 `agentos:reminded:{type}:{obj_id}:{YYYY-MM-DD}` SETNX EX 86400，同一对象同一类提醒每自然日一次。

## 6. 前端（T3.7）

沿用阶段 2 的 shadcn/ui 硬性约定（新增组件仅 `tabs`，由 `npx shadcn@latest add tabs` 生成入库）。

### 6.1 页面结构

- **工作项详情页**挂三个区（`features/collaboration/`）：
  - `CollaborationSection`：协作列表 + 发起协作 Dialog（仅主执行人）；按当前用户身份与状态渲染操作按钮（接受/拒绝/开始/提交回传/要求修改/完成/取消）；每行"详情"Dialog 展示 goal/template/result_text。
  - `TransferSection`：申请转派 Dialog（新负责人/原因/影响说明，有 PENDING 时禁用）+ 转派历史。
  - `DeadlineChangeSection`：DDL 变更申请 Dialog（目标按身份过滤：主任务级=主执行人或负责人，协作级=协作双方且协作活跃）+ 变更历史。
- **features/approvals**：Tabs「待我审批」（仅负责人，`GET /approvals` 卡片，通过/驳回可带意见，详情 Dialog 展示 reason/impact_note 或 impact_analysis 明细，`unavailable` 时提示"未生成 AI 影响分析"）/「我的申请」（我发起的转派 + DDL 变更及状态）。
- **待处理中心**（`features/dashboard/TodoSection`，放在 dashboard 顶部）：后端无独立 todo 接口，由既有列表接口前端聚合五类——待响应/进行中/待修改的收到协作、待我确认的回传、我的 READY 工作项、我审批中的申请进度。dashboard 本身是聚合页且是登录首页，免新增路由。
- **通知**（`features/notifications/NotificationBell`）：顶栏未读数 Badge + DropdownMenu 最近 20 条，点击已读并跳转 link。
- **项目时间线**（`features/dashboard/TimelineSection`，仅负责人）：`GET /audit-events` 事件流，31 个 action 中文映射，actor_id→显示名。

### 6.2 SSE 缓存失效策略

`services/events.ts` 的 `useEventStream()` 在 AppLayout 挂载；accessToken 变化（登录/401 刷新/登出）触发重连或关闭。对 31 种命名事件按 type 前缀失效 TanStack Query 缓存：`work_item.*→work-items`；`collaboration.*→collaboration-requests`；`transfer.*→+approvals+work-items`；`deadline_change.*→+approvals+work-items+collaboration-requests`；`reminder.*→work-items+collaboration-requests` 并 toast.warning；任何事件失效 `notifications` 与 `audit-events`。断线靠 EventSource 自动重连，漏发由"收任意事件即失效相关缓存"兜底。

## 7. 测试与配置

- 全部测试 `docker compose exec backend pytest` → **172 passed**（阶段 2 的 72 个全程无回归）。新增覆盖：协作状态机 28 分支 + 集成 10；转派/DDL 状态机 37 分支 + 集成 18（含 PENDING 冲突 409、审批同事务 assignee+审计+通知、重复审批只生效一次、协作级自动生效两分支、unavailable 仍可审批、approvals 权限）；SSE 4 例（频道事件、最小暴露、认证、端到端帧）；due.scan 3 例（临期/逾期/去重/worker 分发）；详情端点 3 例。
- SSE 端到端测试注意：**httpx 0.28 的 ASGITransport 缓冲整个响应体、不支持流式**，测试改用裸 ASGI 调用真实 app 断言帧；真实传输层由 curl 实测覆盖（uvicorn + nginx 双通道）。
- 新增配置：`DUE_SCAN_INTERVAL_SECONDS`（300，compose/.env.example 已接）、`DUE_SOON_HORIZON_HOURS`（24）。
- 迁移序列追加：`0005_collaboration_notifications` → `0006_transfers_deadline_changes`。

## 8. 已知取舍与阶段 4 衔接

1. 协作级 DDL 变更不设待审批唯一约束（按规格）：同一协作请求两条待审批变更先后获批时，后批覆盖先批，无冲突检测。
2. 审批通过时不重验目标当前状态（如协作请求在审批期间进入终态仍会改其 due_at）。
3. 扫描中崩溃会烧掉当日该条提醒的去重键（次日自愈，未做补偿）；提醒正文时间按 UTC 格式化，前端展示可本地化。
4. 阶段 4（交付与审核）：`result_text` 已支持文本产物回传，文件类产物接入 `stored_files` 后可直接引用；工作项状态机的 `request_changes`/`complete` 迁移与单测已就绪，审核模块只需接 API 端点。
5. 构建提示：清华 PyPI 镜像偶发故障会导致 pip install 失败，遇此可临时 `--build-arg PIP_INDEX_URL=https://pypi.org/simple`；构建脚本注意别让管道吞掉退出码导致镜像静默沿用旧代码。
