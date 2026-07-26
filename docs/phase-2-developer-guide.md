# 阶段 2 开发者指南：身份、成员与工作项

本文面向刚加入 AgentOS 的开发者，说明阶段 2（T2.1–T2.7）实现的**实际形态与设计理由**。文中章节号（如 6.1、8.1、17.2）均指设计文档 `docs/2026-07-26-agentos-workflow-platform-design.md`。阶段 1 的工程基线见 `docs/phase-1-developer-guide.md`，本文不重复。

阶段 2 交付内容：

- 统一 API 错误格式、request_id 透传与 `Idempotency-Key` 幂等机制（T2.1，第 17 章）。
- Argon2 密码 + Access/Refresh Token 登录体系（T2.2，12.1、16 节）。
- 单项目、项目成员与能力模型管理（T2.3，6.1、6.2、12.2 节）。
- `audit_events` 追加式审计基础设施（T2.4，原则 5、16 节）。
- 工作项数据模型、8.1 节状态机与乐观锁（T2.5）。
- 工作项查询与命令 API（T2.6，12.3 节）。
- 前端登录、成员、工作项与看板页面；**前端组件体系从 antd 全面切换为 shadcn/ui**（T2.7，第 13 章，见第 6 节）。

验证基线：`docker compose exec backend pytest` → **72 passed**；六服务全部 healthy。

## 1. 横切机制：错误格式、request_id 与幂等（T2.1，第 17 章）

代码位置：`backend/app/core/`。

### 1.1 统一错误格式（17.1 节）

所有非 2xx 响应统一为 `{"code", "message", "request_id", "details"}`：

- `core/errors.py`：`ApiException(code, message, status_code, details)` 是唯一的业务异常出口；错误码常量化（`INVALID_CREDENTIALS`、`FORBIDDEN`、`WORK_ITEM_VERSION_CONFLICT`、`WORK_ITEM_INVALID_TRANSITION` 等），新错误码在此登记。
- `main.py` 注册全局异常处理器：`ApiException` 按自身 code/status 返回；`RequestValidationError` → 422；`HTTPException` → 404/405 等；未捕获 `Exception` → 500 且**只记录异常类型名**（不记录连接串等敏感信息，16 节）。
- `core/middleware.py` 的 `RequestContextMiddleware`：每请求生成 UUID request_id，写入 `core/request_context.py` 的 contextvars 并回写响应头 `X-Request-ID`。日志、错误响应、审计事件都从 contextvars 自动取用，业务代码无需手传。

### 1.2 Idempotency-Key（17.2 节）

- 持久化在 PostgreSQL 表 `idempotency_records`（key、user_id 可空、method、path、首次响应的状态码与 body JSONB、created_at），唯一索引为 `COALESCE(user_id, 零值UUID) + key + method + path`——幂等键与操作者、接口路径绑定，避免跨用户串用。
- `core/idempotency.py` 提供两个部件：
  - `idempotency_guard` FastAPI 依赖项：请求携带 `Idempotency-Key` 头时查表，命中已存在记录则抛 `IdempotentReplay`，由异常处理器直接返回首次响应（带响应头 `Idempotency-Replayed: true`），**不重复执行业务写入**。
  - 响应落库中间件：首次成功响应写入记录表。
- 用法：命令类接口声明 `Depends(idempotency_guard)` 即启用。与 `get_current_user` 联用时把后者声明在前，守卫可自动填充 user_id。
- 已验证：同一幂等键重复调用只执行一次业务写入（Redis 队列长度/审计事件条数断言），第二次返回首次结果。

### 1.3 乐观锁约定

更新接口要求客户端携带 `version`；不匹配返回 409 `WORK_ITEM_VERSION_CONFLICT`，`details.current_version` 带当前版本号。机制在 T2.1 定义，在 T2.5/T2.6 的工作项上正式启用。

## 2. 认证体系（T2.2，12.1、16 节）

代码位置：`backend/app/domains/identity/`。

### 2.1 数据模型（迁移 0002）

- `users`：username（唯一）、password_hash、is_active、token_version。
- `refresh_tokens`：user_id、token_hash（SHA-256）、expires_at、revoked_at。**库中不存在明文密码与明文 refresh token。**

### 2.2 密码与令牌

- `security.py`：密码用 **Argon2id**（`argon2-cffi`）哈希/校验；Access Token 为 JWT（`PyJWT`，载荷含 `sub`、`tv`=token_version、`type`、`exp`）；refresh token 为 `secrets.token_urlsafe(48)` 随机串，只存 SHA-256 哈希。
- 安全规则（16 节）：Access Token 短期有效（默认 30 分钟）；refresh 时旧 refresh token 立即作废（轮换）；登出即撤销；禁用用户或提升 `users.token_version` 后旧 Access Token 失效（校验载荷 `tv` 与库中值）。
- `dependencies.py` 的 `get_current_user`：解析 Bearer token、校验 is_active 与 token_version，是后续所有接口的身份入口。

### 2.3 接口

| 接口 | 请求 | 响应 |
|---|---|---|
| `POST /api/v1/auth/login` | `{username, password}` | `{access_token, refresh_token, token_type, expires_in}`；401 `INVALID_CREDENTIALS`；403 `USER_DISABLED` |
| `POST /api/v1/auth/refresh` | `{refresh_token}` | 新令牌对；401 `REFRESH_TOKEN_INVALID` |
| `POST /api/v1/auth/logout` | `{refresh_token}` | `{status:"ok"}`（支持幂等键） |
| `GET /api/v1/auth/me` | Bearer | `{id, username, is_active, created_at}`（不含角色，角色见 3.3 节） |

### 2.4 Bootstrap 初始账号

首版不开放公开注册，但新环境需要第一个负责人。`app/scripts/bootstrap.py`（容器启动命令中 `alembic upgrade head` 之后自动执行，幂等）完成三件事：

1. 从 `BOOTSTRAP_ADMIN_USERNAME` / `BOOTSTRAP_ADMIN_PASSWORD`（开发默认 `admin` / `admin123`，生产必须覆盖）创建初始账号；
2. 创建默认项目（`BOOTSTRAP_PROJECT_NAME`，默认 "AgentOS 项目"）；
3. 把初始账号登记为该项目的 **leader** 成员（`BOOTSTRAP_ADMIN_DISPLAY_NAME`）。

### 2.5 审计查询入口（T2.4 附带）

`GET /api/v1/audit-events?limit=&offset=`：**仅项目负责人**（普通成员 403、匿名 401）。返回操作者、动作、目标、before/after 摘要、request_id、来源 IP、时间。

## 3. 成员与能力模型（T2.3，6.1、6.2、12.2 节）

代码位置：`backend/app/domains/project/`（`models.py`、`service.py`、`dependencies.py`、`router.py`）。

### 3.1 数据模型（迁移 0003）

- `projects`：单项目配置，首版一条记录。
- `project_members`：project_id、user_id（唯一关联 users）、role（`leader`/`member`）、display_name、weekly_available_hours、git_username、is_active。
- `member_capabilities`：member_id、tag、proficiency（1–5）、confirmed、confirmed_by_member_id、confirmed_at。

### 3.2 权限策略

集中在 `service.py`（4.1 节"权限策略集中在应用服务层"），`dependencies.py` 提供 `get_current_member` / `get_current_leader`：

- 仅负责人：创建成员（同时生成登录账号，**初始密码只在创建响应中返回一次**）、编辑资料、禁用/启用（联动 `users.is_active`，禁用后即无法登录）、确认能力。
- 成员本人：填报/修改自己的能力，提交后 confirmed 复位为未确认，待负责人确认（6.2 节）。
- 任何项目成员：`GET /members` 返回全员摘要（含能力与 `active_work_items` 负载统计），**不含密码哈希、令牌等敏感字段**（透明原则 6 与 16 节）。
- 成员创建/维护/能力确认均同事务写审计事件（`member.created`、`capabilities.submitted`、`capabilities.confirmed` 等）。

### 3.3 当前用户的角色判定

`GET /auth/me` 只返回账号信息。前端拿到 `id`（user_id）后在 `GET /members` 中匹配 `user_id`，得到本人的 member 记录（role、member id），据此控制界面显隐。

## 4. 工作项：模型、状态机与命令 API（T2.5/T2.6）

代码位置：`backend/app/domains/work_items/`。

### 4.1 数据模型（迁移 0004）

- `work_items`：title、description、acceptance_criteria、priority（low/medium/high/urgent）、assignee_id → project_members、due_at、status、`version`（VersionMixin 乐观锁）。
- `work_item_collaborators`：work_item_id + member_id 唯一对。主责任唯一（原则 4）：assignee 单字段，协作者走关联表。

### 4.2 状态机（8.1 节）

`state_machine.py` 是纯函数模块（可单测，不依赖数据库）：`transition(current_status, command) -> new_status`，7 个状态、8 个命令，非法迁移抛 `ApiException(409, WORK_ITEM_INVALID_TRANSITION)`。合法迁移：

```text
DRAFT --publish--> READY --start--> IN_PROGRESS --submit--> IN_REVIEW
IN_PROGRESS <--block/unblock--> BLOCKED
IN_REVIEW --request_changes--> IN_PROGRESS   IN_REVIEW --complete--> COMPLETED
DRAFT/READY/IN_PROGRESS --cancel--> CANCELLED
```

`request_changes`/`complete` 迁移与单测已就绪，API 端点留待阶段 4 审核模块接入。单元测试覆盖全部合法迁移与 23 个非法分支。

### 4.3 接口与权限（12.3 节）

| 接口 | 权限 | 说明 |
|---|---|---|
| `GET /work-items?assignee_id=&status=&due_from=&due_to=` | 任何成员 | 全量列表摘要（标题/状态/负责人/优先级/DDL/version），透明原则 6 |
| `POST /work-items` | 负责人 | 创建（含 assignee_id、可选 collaborator_ids），初始 DRAFT |
| `GET /work-items/{id}` | 任何成员 | 完整详情 |
| `PATCH /work-items/{id}` | 负责人 | 携带 `version`，未提供字段不变 |
| `POST /work-items/{id}/publish` | 负责人 | 发布 |
| `POST /work-items/{id}/start` `block` `unblock` `submit` | 当前主执行人 | 均携带 `{version}`；submit 只推进到 IN_REVIEW |
| `POST /work-items/{id}/cancel` | 负责人 | 取消 |

WorkItemOut：`{id, title, description, acceptance_criteria, priority, status, assignee:{id,display_name}, collaborators:[{id,display_name}], due_at, version, created_at, updated_at}`。

并发与留痕规则（17.2 节）：

- 所有命令/PATCH 支持 `Idempotency-Key`；成功更新 `version+1`；version 不匹配返回 409，`details.current_version` 供客户端刷新。
- 每次状态迁移/字段变更与审计事件**同一个数据库事务**写入（原则 5）；`assignee_id` 变化必须留痕，为设计文档第 22 章标准 2"历史负责人完整可查"打底。
- 已验证：同一幂等键重复 `start` 只产生一次状态变化和一条审计事件；过期 version 收到 409。

## 5. 审计基础设施（T2.4，原则 5、16 节）

代码位置：`backend/app/domains/audit/`。

- `audit_events` 表（迁移 0002）：actor_id、action、target_type、target_id、before/after（JSONB 变更摘要）、request_id、source_ip、created_at。**只有 created_at 没有 updated_at**——追加式语义落在模型层面。
- `service.py` 的 `record_event(session, actor_id, action, target_type, target_id, before, after)`：request_id 与来源 IP 从 contextvars 自动填充；**只 flush 不 commit**，由业务用例统一 commit，从而与业务写入同生共死（已有测试验证：模拟事件写入失败时业务写入回滚）。
- 全项目不存在任何更新/删除审计事件的代码路径；查询入口仅 2.5 节的只读接口。

## 6. 前端（T2.7，第 13 章）

### 6.1 组件体系：shadcn/ui（硬性约定）

**本项目前端使用 shadcn/ui，严格使用对应组件，绝不越界**（用户明确决策，后续阶段必须遵守）：

- antd 已彻底移除（依赖与 import 零残留）。一切 UI 元素必须使用 `frontend/src/components/ui/` 下由 `npx shadcn@latest add` 生成并入库的官方组件：badge、button、card、checkbox、dialog、dropdown-menu、form、input、label、select、separator、skeleton、sonner、table、textarea。
- shadcn 有对应组件就不准手写替代品；表单一律用 shadcn Form（react-hook-form + zod）；反馈一律用 Sonner；布局用 Tailwind 工具类；图标用 `lucide-react`。
- 需要新组件时执行 `npx shadcn@latest add <组件>`（宿主机 Node 22 可用，registry 走 npmmirror，见 `frontend/.npmrc`），生成物入库——Docker 构建不依赖 CLI。
- 技术底座：Tailwind v4 + `@tailwindcss/vite` 插件（`vite.config.ts`），`@` 路径别名，主题变量在 `src/index.css`。

保留不变的技术栈：Vite 6 + React 18 + TypeScript 5、react-router-dom 6、TanStack Query 5、zustand 5。

### 6.2 会话与 API 客户端

- `app/store.ts`：zustand + persist（localStorage）存 access/refresh token、当前 user 与 member；导出 `useIsLeader`。
- `features/auth/session.ts` 的 `loadIdentity()`：`/auth/me` + `/members` 按 user_id 匹配本人成员记录（3.3 节）。
- `services/api.ts`：401 时单例 refresh 并重试一次（失败清登录态跳 /login）；`newIdempotencyKey()` 用 `crypto.randomUUID()` 生成幂等键——**同一操作的自动重试复用同一键，用户重新点击生成新键**；409 透出 `ApiError.isVersionConflict`，统一提示"任务已被其他成员更新，请刷新后重试"并刷新数据。
- `components/RequireAuth.tsx`：路由守卫；`components/AppLayout.tsx`：Tailwind 侧边导航 + 顶栏用户/角色 Badge + DropdownMenu 登出（调 `/auth/logout` 撤销 refresh token）。

### 6.3 页面

- `features/auth/LoginPage.tsx`：shadcn Form 登录页，401 提示"用户名或密码错误"。
- `features/members/`：成员 Table（角色/能力 Badge 含待确认态、活跃任务数、可投入时间）；负责人可创建成员（成功后 Dialog 展示一次性 initial_password）、编辑、禁用/启用、一键确认能力；成员本人"填报我的能力"（动态行 + 1–5 熟练度 Select）。
- `features/work-items/`：列表页（过滤：负责人/状态/DDL 区间，走后端 query 参数）；详情页含命令按钮——显隐规则为：发布=负责人+DRAFT，开始/阻塞/解除阻塞/提交=主执行人+对应状态，取消=负责人+未终态；均携带当前 version 与幂等键。创建/编辑共用 `work-item-form.tsx` Dialog 表单。
- `features/dashboard/DashboardPage.tsx`：团队透明看板雏形（13.2 节）——状态分布卡片、全员工作量表、7 天内到期（逾期标红）列表，由 `GET /members` + `GET /work-items` 前端聚合（`GET /dashboard` 接口留待后续阶段）。
- approvals / deliverables / agent-assistant 为 shadcn Card 占位页，分别对应阶段 3/4/5。

## 7. 测试（第 18 章）

全部测试在容器内运行：`docker compose exec backend pytest`（**72 passed**）。

- `tests/conftest.py`：独立测试库 `agentos_test`（导入 app 前改写 `DATABASE_URL`，Redis 切 db 15 避开 worker/scheduler），自动建库 + `alembic upgrade head`，用例间 TRUNCATE 隔离，**不污染 `agentos` 主库**；提供 `project`/`leader` fixture 与 `add_member`/`auth_headers` 辅助函数。每用例结束 dispose 引擎，规避 pytest-asyncio 跨事件循环连接复用问题。
- 覆盖：错误格式（4）、认证（9：登录/错误密码/禁用/refresh 轮换/logout 失效/token_version 失效/无明文存储）、幂等（2）、审计（3：全字段、事务同生共死、权限）、状态机（全部合法 + 非法分支）、成员（8：403/确认流转/禁用联动）、工作项 API（10：全命令流/过滤/409/幂等重放/assignee 留痕/负载统计）。
- **改了代码或测试需重建镜像才会进入容器**：`docker compose build backend worker scheduler && docker compose up -d`。

### 实现陷阱备忘（后续阶段复用）

1. 双外键指向同一张表时，SQLAlchemy relationship 需 `foreign_keys` 消歧。
2. `updated_at` 因 `onupdate=func.now()` 在 UPDATE 后属性过期，commit 后需 `session.refresh()` 再序列化。
3. 唯一约束下"替换集合"（如能力列表、协作者列表）必须先清空 flush 再写入，否则同事务先插后删冲突。

## 8. 配置项汇总（新增）

`.env.example` 已同步，默认值即可跑通开发环境；生产必须覆盖 `JWT_SECRET` 与 bootstrap 密码。

| 配置项 | 含义 | 默认 |
|---|---|---|
| `JWT_SECRET` | JWT 签名密钥 | `dev-jwt-secret-change-in-production` |
| `JWT_ALGORITHM` | 签名算法 | `HS256` |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | Access Token 有效期 | `30` |
| `REFRESH_TOKEN_EXPIRE_DAYS` | Refresh Token 有效期 | `14` |
| `BOOTSTRAP_ADMIN_USERNAME` / `BOOTSTRAP_ADMIN_PASSWORD` | 初始负责人账号 | `admin` / `admin123` |
| `BOOTSTRAP_PROJECT_NAME` / `BOOTSTRAP_ADMIN_DISPLAY_NAME` | 默认项目与负责人显示名 | `AgentOS 项目` / `项目负责人` |

迁移序列：`0001_baseline`（pgcrypto）→ `0002_identity_audit_idempotency` → `0003_project_members` → `0004_work_items`。容器启动自动 `alembic upgrade head && python -m app.scripts.bootstrap && uvicorn ...`。

## 9. 常用验证命令

```bash
docker compose up -d && docker compose ps          # 六服务应全部 healthy
docker compose exec backend pytest                 # 72 passed

# 登录（admin / admin123），经前端 nginx 反代或直连 backend
TOKEN=$(curl -s -X POST http://localhost:3000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin123"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
curl -s http://localhost:3000/api/v1/members -H "Authorization: Bearer $TOKEN"
curl -s http://localhost:3000/api/v1/work-items -H "Authorization: Bearer $TOKEN"
curl -s http://localhost:3000/api/v1/audit-events -H "Authorization: Bearer $TOKEN"   # 仅负责人

# 幂等验证：同一 Idempotency-Key 重复调用，第二次带 Idempotency-Replayed: true
curl -i -X POST http://localhost:8000/api/v1/work-items/<id>/start \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $(uuidgen)" -d '{"version": 2}'
```

## 10. 阶段 3 衔接点

- 协作请求（collaboration）、转派审批（transfers）、DDL 变更（deadlines）、站内通知与 SSE（notifications）：领域包已就位，状态机见设计文档 8.2–8.4 节；本阶段的幂等、乐观锁、审计、权限依赖项直接复用。
- `GET /audit-events` 已按负责人权限收紧，阶段 6 审计测试可直接基于现有数据。
- 前端新增 UI 一律走 shadcn/ui（6.1 节）；待办中心、审批中心页面挂入现有 AppLayout 导航即可。
