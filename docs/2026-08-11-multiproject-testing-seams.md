# 多项目改造 · 测试接缝设计

> 日期：2026-08-11
> 关联：多项目改造 spec（to-spec 产出）；决策来源：grill-with-docs

## 一、什么是测试接缝

**测试接缝**（testing seam）是"从最外层去验证功能行为的那条路径"。

原则：
- 只测**外部行为**，不测实现细节（不 mock 到服务层内部、不验证"某函数被调用了几次"）
- 选**尽可能高的接缝**——能在外层验证的，就不在内层建测试
- 接缝**越少越好**，理想状态是整件事只有一条
- **优先复用既有接缝**，不轻易新建

对多项目改造而言：核心行为是"**跨项目隔离**"——这天然是系统最外层的行为（一个请求带着项目上下文进来，系统正确响应），所以应该在**HTTP API 层**验证，而不是在 service 或状态机层。

## 二、接缝总览

| 接缝 | 层级 | 验证什么 | 是否新建 |
|---|---|---|---|
| **主接缝** | 后端 HTTP API（pytest + httpx） | 多项目核心正确性：隔离、越权、header 语义、幂等、admin 全局化 | 扩展现有接缝 |
| **副接缝** | 前端 Vitest 流程测试 | UI 行为：登录分流、项目选择页、store 项目化 | 扩展现有接缝 |

两条都**复用既有接缝**，不新建测试层级。

## 三、主接缝：后端 HTTP API 层

### 为什么是它

现有测试（`backend/tests/`）已经全部走 HTTP 层：`conftest.py` 提供真实测试库、`client`（httpx.AsyncClient）、`project`/`add_member`/`leader`/`auth_headers` fixture。多项目改造的行为——"带着 A 项目的请求看不到 B 项目的数据"——**只有通过完整 HTTP 请求才能真实验证**（header 携带、依赖解析、service 过滤、越权响应全链路）。

### 扩展方式

现有 `conftest` 增量扩展，不推翻：

- `project` fixture 参数化出**两个项目**：A 项目、B 项目（各自独立的项目记录）
- `add_member` 支持**跨项目建成员**：同一用户可以既是 A 项目 leader、又是 B 项目 member
- `auth_headers` 扩展为同时携带 `Authorization` + **`X-Project-Id`**，按测试需要指向 A 或 B

### 验证的行为矩阵

| 行为 | 断言 |
|---|---|
| 隔离 | A 项目的任务列表不含 B 项目的任务；交付物/通知/文件同理 |
| 对象越权 | 用 A 的 header 访问 B 的任务详情/操作 → **404** |
| header 缺失 | 不带头 → **400** |
| header 越权 | 带一个自己不是成员的项目 → **403** |
| 跨实体引用校验 | 把 A 项目的成员指派为 B 项目任务的 assignee → **400** |
| 幂等键 | 同一用户、同一幂等键，跨 A/B 两项目 → **不复用响应**（project_id 并入唯一键） |
| admin 全局化 | admin 无成员记录仍可访问管理接口；`get_current_admin` 校验；审计接口对 admin 放行 |
| worker 写路径 | due_scan/risk_scan 从 work_item/run 推导 project_id 正确落库 |
| 项目级派生 | deliverables/审批项经 work_item 推导项目正确；agent_suggestions 经 run 推导正确 |

### Prior Art

现有 API 级测试直接复用其模式：
- `backend/tests/test_transfers_api.py`、`test_reviews_api.py`、`test_members.py`、`test_agent_suggestions_api.py`
- `conftest.py` 的 `client`/`project`/`add_member`/`auth_headers`

## 四、副接缝：前端 Vitest 流程测试

### 为什么需要它

UI 行为（登录后去哪、项目选择页渲染、切换项目、角色区块显隐）**无法用后端接缝验证**，必须在组件/流程层测。

### 覆盖的行为

| 行为 | 断言 |
|---|---|
| 登录分流 | admin 登录 → 管理控制台；普通用户 → 项目选择页（24h 记忆逻辑） |
| 24h 记忆 | 有效期内直接进上次项目；超时/首次 → 选择页 |
| 项目选择页 | 列出"我参与的项目"、角色徽章、点选进入 |
| store 项目化 | 切换项目后 `useIsLeader`/`useIsAdmin` 跟随当前项目 |
| API 客户端 | 请求自动带 `X-Project-Id`；SSE URL 带 project_id |
| admin 角色来源 | `user.is_admin`（不再读 member.role） |

### Prior Art

- `frontend/src/__tests__/member-flow.test.tsx`、`leader-flow.test.tsx`、`admin-role.test.tsx`、`e2e/full-workflow.test.tsx`
- 组件测试：`features/*/__tests__/`

## 五、为什么不新建接缝

- **不在 service 层建独立测试层**：多项目行为是"跨请求隔离"，HTTP 层已覆盖全链路；service 层再测一遍是重复。
- **状态机不涉及**：状态迁移逻辑与项目无关（转派/工作项状态机是纯函数），不受多项目影响，不需要动。
- **不在数据库/迁移层测业务**：迁移正确性靠重建 + API 测试回归，不单设层。

理想接缝数为 1（后端 API）；前端是必要的最小补充（UI 行为），合计 2 条，不扩散。

## 六、接缝的边界（明确不测的）

- 不测 Provider/模型调用细节（Ollama/OpenAI 层无项目上下文，无关）
- 不测幂等键的底层存储，只测跨项目行为
- 不测 24h 过期的时间流逝本身（用注入时间/直接断言分支），只测两种分支行为
- 前端不测真实后端联调（沿用 mock API 的既有模式）

## 七、这条设计对实现的意义

测试接缝决定了实现的顺序依赖：
1. 先落 **数据层**（6 张表 project_id + conftest 双项目 fixture）——主接缝的地基
2. 再落 **身份/权限**（X-Project-Id、admin 全局化）——主接缝的核心断言
3. 再落 **接口层**（过滤/归属校验/幂等）
4. 最后 **前端**（store/分流/选择页）——副接缝

每步完成即跑主接缝回归，保证接缝随时可执行、不积压到最后一刻。
