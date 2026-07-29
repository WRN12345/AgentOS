# 阶段 6 开发者指南：质量与部署

本文面向刚加入 AgentOS 的开发者，说明阶段 6（T6.1–T6.7）实现的**实际形态与设计理由**。文中章节号指设计文档 `docs/2026-07-26-agentos-workflow-platform-design.md`。阶段 1–5 的机制见 `docs/phase-1/2/3/4/5-developer-guide.md`，本文不重复。

阶段 6 交付内容：

- 后端五维测试体系（单元/API 集成/并发/Agent 合约/审计），并修复两个真实并发缺陷（T6.1，18.1 节）。
- 前端 Vitest 测试体系：组件、页面集成、e2e 骨架（T6.2，18.2 节）。
- RAG 串行与双任务并行两个端到端验收场景（T6.3/T6.4，第 9 章、18.3 节）。
- 备份/恢复脚本与真实恢复演练（T6.5，19.4 节）。
- 安全检查记录（T6.6，第 16 章）。
- 性能基线、内部发布指南、MVP 标准核对清单（T6.7，第 20、22 章）。

验证基线：后端 `pytest tests/` → **312 passed**（阶段 5 的 261 个全部无回归）；前端 `npm run test` → **57 passed / 5 todo**；`npm run build`（tsc strict）零错误；两个 e2e 场景连跑 10 遍稳定；六服务重建后全部 healthy。

## 1. 后端测试体系（T6.1，18.1 节）

### 1.1 运行方式

```bash
docker compose run --rm --no-deps -v "$PWD/backend:/app" backend python -m pytest tests/ -q
```

- 容器内执行（postgres/redis 只在 Compose 网络内可达），`-v` 挂载宿主机源码保证跑的是实时代码（镜像内是构建时快照）。
- `conftest.py` 在任何 app 导入前把 `DATABASE_URL` 库名改写为 `<原名>_test`、Redis 切到 db 15；会话级自动建库 + `alembic upgrade head`；每个用例后 TRUNCATE 全部业务表（含 LangGraph 检查点表，存在才清）。
- **并行跑多套测试**：给 `DATABASE_URL` 指定不同库名、给 `REDIS_URL` 指定非 0 的 db（如 13/14）即可互相隔离——`conftest.py` 只在 Redis 路径为 `/0` 或空时才强制改写为 `/15`（阶段 6 的小改动，此前一律强制 15）。

### 1.2 五个维度与新增文件

| 维度 | 文件 | 覆盖点 |
| --- | --- | --- |
| 领域单元 | `test_unit_permissions.py`（6）、`test_unit_transfer_rules.py`（2）、`test_unit_deadline_rules.py`（6） | 权限策略缺口（非主执行人/无关第三方/禁用成员分支）；7.3 节转派规则（主责任转移时点、连续转派 from/to 链）；7.4 节 DDL 影响规则（≤ 主任务 DDL 自动生效、无 DDL 边界、协作级多待审批并存） |
| API 集成 | `test_api_gaps_t6b.py`（12） | 对照第 12 章逐端点补缺：404/422/409 错误分支、六命令端点对不存在资源、BLOCKED 取消 409、三类审批的版本冲突 |
| 并发 | `test_concurrency.py`（4） | `asyncio.gather` 真并发：重复审批确定性 [200,409]、乐观锁同版本 PATCH 一胜一负、同幂等键并发创建只建一条 |
| Agent 合约 | `test_agent_contract.py`（6） | `StubModelProvider` 替身（不依赖 Ollama）：合法输出成功且系统侧权威字段覆盖模型自报、Schema 非法/非 JSON/JSON 数组走诊断落 failed、超时不可用落 failed；**所有失败路径零建议、零通知、零新增业务审计**（17.3 节） |
| 审计 | `test_audit_coverage.py`（9） | 9 类关键动作逐一断言审计动作/actor/target/before-after；审计不可变（写方法 405/404）；已实证"删除任一审计写入，对应测试必失败" |

辅助模块（tests 包内，可 import）：`helpers_t6a.py`（场景构造 + storage fixture）、`helpers_t6b.py`（agent run 驱动）、`helpers_e2e.py`（e2e 场景共用：替身 Provider、业务状态快照、审计回放断言）。新增测试优先复用这些 helper，不要再改 `conftest.py`。

### 1.3 顺带修复的两个真实并发缺陷（17.2 节）

并发测试先用探针实证了缺陷，再做最小修复：

1. **乐观锁并发失效**：transfers/deadlines/work_items 的版本检查原是"应用层读后检查"，交错窗口下两个 approve 都返回 200。修复：`get_request()`/`get_work_item()` 增加 `for_update=True`（`SELECT ... FOR UPDATE`），仅写路径启用（8 个调用点），行锁把并发写串行化，后到者重读新版本后被既有版本检查挡下返回 409。
2. **幂等键并发穿透**：原实现"先查后写"，同键并发各建一条记录。修复：`app/core/idempotency.py` 重写为**占位预约模式**——守卫先插入 `response_status=0` 占位记录抢占执行权（唯一索引兜底），其余请求有界等待（10s、50ms 轮询）后重放首次响应；中间件把首次响应写回占位；5xx/异常删除占位允许重试。新增错误码 `IDEMPOTENCY_IN_PROGRESS`。串行重放语义不变（`test_idempotency.py` 全数通过）。

已知同类残留（记录在案，未超范围修复）：`collaboration/service.py` 的版本检查仍是应用层读后检查，存在同型竞态，建议后续版本同样改行锁。

## 2. 前端测试体系（T6.2，18.2 节）

### 2.1 基建

- 新增 devDependencies：`vitest`、`@testing-library/react` / `user-event` / `jest-dom` / `dom`、`jsdom`（未升级任何既有依赖）。
- `frontend/vitest.config.ts`（jsdom 环境、`@` alias）；`src/test/setup.ts`（jest-dom + Radix polyfill + 每用例清理登录态）；`src/test/mock-api.ts`（`vi.mock` 替换 `services/api` 的 `api` 对象，保留真实 `ApiError`）；`src/test/fixtures.ts`（夹具工厂）；`src/test/render.tsx`（`renderWithProviders` / `signInAs`）。
- 脚本：`npm run test`（`vitest run`）、`npm run test:watch`。

### 2.2 覆盖

- **组件测试**（被测组件旁 `__tests__/`）：登录表单（5）、创建工作项表单（5，含幂等键断言与 409 冲突提示）、协作请求（4）、DDL 变更（4）、状态徽标（16，七状态四优先级全映射）、审批卡片（4，含成员视角无审批 tab）、文件上传（4，前端前置校验不发请求）。
- **页面集成**（`src/__tests__/`）：`leader-flow.test.tsx`（5，建成员→建工作项→审批→审核）与 `member-flow.test.tsx`（5，看板→协作→提交交付）；权限差异有断言（成员看不到创建工作项/审批入口，非主执行人看不到提交交付）。
- **e2e 骨架**：`src/__tests__/e2e/full-workflow.test.tsx`（5 可跑 + 5 `it.todo`），文件顶部注释固定与后端 T6.3 场景共用的 7 步时序及每步接口约定（端点/body/幂等键/乐观锁语义），依赖真实后端状态机的步骤留 TODO。

## 3. 端到端验收场景（T6.3/T6.4，第 9 章、18.3 节）

两个场景都是 pytest 文件、单测试函数内跑完整时序（conftest 每用例后清库，跨函数不保留状态），与后端测试同一键入口。

### 3.1 `test_e2e_rag_serial.py`（T6.3）

按第 9 章时序：负责人分配 RAG 工作项（后端开发）→ 申请转派给 RAG 工程师 → 负责人审批（主责任转移、历史负责人可查）→ 两次协作（整理资料、标注测试集，含文件回传）→ 提交 Git 链接 + 评估文本 + 说明文件 → 审核通过 COMPLETED → 归档证据文件。逐步断言审计事件与通知；协作无需审批（负责人待审批列表始终为空）、转派必须审批（审批前主责任不变）；场景中段用替身 Provider 驱动 Agent run，**运行前后业务状态快照完全一致**（`snapshot_business_state`：工作项 status/version + 审计 id 集合）。末尾 `assert_audit_replay` 按审计事件重建完整时序与 25 步预期逐一比对。

### 3.2 `test_e2e_parallel.py`（T6.4）

RAG 任务与 Agent 工具设计任务由不同成员**并行**推进（`asyncio.gather` 交错每一步）：并行创建（同幂等键并发重复提交只建一个）、并行发布/开始/协作/交付/送审、负责人并发审核两个工作项各自独立通过。断言：两个任务的审计目标集合不相交、协作者通知互不串扰、成员负载在推进中=1/完成后=0、Agent 建议不改业务状态、两个任务各自审计链可独立回放。

### 3.3 一个值得知道的坑

`assert_audit_replay` 的"同刻分组"（同事务事件 `created_at` 相同，组内无序）初版分组逻辑有 bug：把步骤元组的 action 字符串拿去和时间戳比较，分组从未生效，测试靠 UUID 随机排序碰巧通过（约 50% 偶发失败）。已修复为单独记录分组 key，修复后连跑 10 遍稳定。**凡涉及并发/时序断言，先证明它能稳定失败再相信它能稳定通过。**

## 4. 备份与恢复（T6.5，19.4 节）

- `deploy/scripts/backup.sh`：pg_dump 自定义格式 → `data/backups/postgres/`；`tar --listed-incremental` 增量 → `data/backups/uploads/`；14 天保留自动清理；日志写 `data/logs/backup.log`。配置取环境变量/.env，默认与 compose 一致。
- `deploy/scripts/restore.sh`：恢复到**指定目标库**（覆盖主库必须 `--confirm`，已实测拒绝）；恢复后自动校验：库可连、核心表存在、`stored_files` 随机抽查 SHA-256 与实际文件比对。
- 定时触发：宿主机 crontab（`deploy/scripts/README.md` 有配置方法，未代为安装系统 cron）。
- **恢复演练**（标准 12 证据）：`docs/restore-drill-2026-07-28.md`——造数 → 备份 → 恢复到全新库+目录 → 业务数据可查、SHA-256 抽查 2/2 一致 → 清理演练环境。保留策略用 `touch -d` 构造超期文件实测清理生效。
- 遗留说明：增量包只含当次变更，精确恢复到某天需按序解包"全量基线 + 增量包"（README 已写明；MVP 上传体量极小，风险可接受）。

## 5. 安全检查（T6.6，第 16 章）

`docs/security-checklist-2026-07-29.md`：凭据（Argon2id 实测、Refresh Token 哈希+可撤销、无公开注册入口）、权限（13 个 router 全部显式鉴权、未认证实测 5 端点全 401、文件下载与透明范围有测试覆盖）、日志（logger 调用与日志文件扫描均无密码/令牌/Key/文件原文）、模型最小上下文与云端提示（前端两处外发警示）、网络面（compose postgres/redis 未发布端口实测）。**全部通过，无需代码修复项**；唯一环境提醒：宿主机系统级 postgres（非 AgentOS 组件）监听 0.0.0.0:5432，建议管理员收紧。

## 6. 性能基线与发布文档（T6.7）

- `backend/scripts/benchmark.py`：可重复运行的基线脚本（幂等种子 `perf_` 前缀成员/工作项），输出 Markdown 表格。基线报告 `docs/perf-baseline-2026-07-28.md`：101 工作项量级下读接口 p95 < 35ms、命令 p95 < 70ms、登录约 96ms（Argon2 成本属预期）、SSE 建立 < 16ms；**未发现慢查询，未新增索引**（结论与复测方法见报告）。
- `docs/release-guide.md`：Debian 宿主机准备（19.1）、标准/快速两种开发模式（19.3）、`.env.example` 逐项说明、备份恢复入口、已知限制（2.2）、常见问题、部署验证记录（标准 11：build → down/up → 六 healthy → 登录实测）。
- `docs/mvp-checklist.md`：第 22 章 13 条标准逐条核对 + 证据指针，**全部满足，MVP 宣告完成**。

## 7. 测试与配置汇总

- 后端 312 passed = 阶段 5 的 261 + 新增 51（单元 14、API 补缺 12、并发 4、合约 6、审计 9、e2e 2，另有既有文件随并发修复小幅补充）。
- 前端 57 passed / 5 todo（10 个文件）；`npm run build`（tsc strict）零错误。
- 新增脚本：`deploy/scripts/backup.sh`、`deploy/scripts/restore.sh`、`backend/scripts/benchmark.py`。
- 新增文档：`docs/restore-drill-2026-07-28.md`、`docs/security-checklist-2026-07-29.md`、`docs/perf-baseline-2026-07-28.md`、`docs/release-guide.md`、`docs/mvp-checklist.md`。
- 无新增迁移、无新增运行时依赖；后端改动集中在 `core/idempotency.py`（重写）与 transfers/deadlines/work_items 的 `for_update` 行锁。

## 8. 已知取舍与后续建议

1. `collaboration/service.py` 乐观锁仍是应用层读后检查，建议照 transfers 范本补 `for_update`。
2. 设计 12.6 的 `GET /dashboard` 后端未实现，前端用 `/members` + `/work-items` 聚合替代——属实现与文档的有意偏差，建议回写设计文档。
3. e2e 场景中"飞书同步勾选"是系统外手工步骤，平台以 COMPLETED 终态 + 归档证据文件表达（场景注释已说明）。
4. 上传目录增量备份的单包恢复需按序解包，如上传体量增长可改周期全量 + 增量混合策略。
5. 性能基线数据量（百级工作项）离生产还有一个数量级，增长后按基线报告第 4 节复测，首要关注负载聚合与审计表扫描。
