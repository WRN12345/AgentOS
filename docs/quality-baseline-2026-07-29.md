# AgentOS 质量基线（MVP 完成证据汇总）

整合自原 mvp-checklist.md、perf-baseline-2026-07-28.md、restore-drill-2026-07-28.md、security-checklist-2026-07-29.md 四份文档。MVP 于 2026-07-29 宣告完成，本文档汇总四类质量证据，原文档可通过 git 历史追溯。

## 1. MVP 完成标准核对

核对日期：2026-07-29。13 条标准逐条核对，证据指针指向具体测试 / 文档 / 脚本。

| # | 标准 | 结论 | 证据 |
| --- | --- | --- | --- |
| 1 | 负责人能创建成员和工作项 | ✅ | `tests/test_members.py`、`tests/test_work_items_api.py`；前端 `src/__tests__/leader-flow.test.tsx` |
| 2 | 成员能申请转派，负责人能审批，历史负责人完整可查 | ✅ | `tests/test_transfers_api.py`、`tests/test_unit_transfer_rules.py`（连续两次转派的 from/to 链）；e2e `tests/test_e2e_rag_serial.py` 步骤 2–3 |
| 3 | 成员能直接发起协作请求并完成产物回传 | ✅ | `tests/test_collaboration_api.py`；e2e 串行场景步骤 5–7（两次协作含文件回传） |
| 4 | DDL 变更正确区分协作级与主任务级审批 | ✅ | `tests/test_deadlines_api.py`、`tests/test_unit_deadline_rules.py`（≤ 主任务 DDL 自动生效 / 超出走审批） |
| 5 | Git 链接、文本、文件能版本化提交 | ✅ | `tests/test_deliverables_api.py`；e2e 串行场景步骤 9（三类交付物版本 1/2/3 断言） |
| 6 | 负责人能要求修改或最终通过 | ✅ | `tests/test_reviews_api.py`、`tests/test_audit_coverage.py`（打回→重交→通过） |
| 7 | RAG 串行案例和双任务并行案例能端到端运行 | ✅ | `tests/test_e2e_rag_serial.py`、`tests/test_e2e_parallel.py`（连跑 10 遍稳定通过，含审计回放逐步比对） |
| 8 | 所有关键操作都有不可覆盖的审计事件 | ✅ | `tests/test_audit.py`、`tests/test_audit_coverage.py`（9 类关键动作逐一断言 + 不可变断言；已实证删除任一审计写入对应测试必失败） |
| 9 | Ollama 不可用时核心工作流仍可正常使用 | ✅ | `tests/test_agent_retry.py`（worker 韧性）、`tests/test_agent_contract.py`（超时/不可用落 failed 不污染业务状态） |
| 10 | Agent 不具备改变正式业务状态的工具 | ✅ | `tests/test_agent_guardrails.py`（工具注册表无写业务工具）；两个 e2e 场景的 Agent 运行前后业务状态快照比对 |
| 11 | Docker Compose 能在 Debian 上启动全部应用服务 | ✅ | `docs/release-guide.md` 第 10 节部署验证记录（build → down/up → 六服务 healthy → 登录实测） |
| 12 | 数据库和文件备份能完成一次实际恢复 | ✅ | 本文档第 3 节（恢复到全新库+目录，SHA-256 抽查 2/2 一致） |
| 13 | 成员能看全员工作量/状态/摘要，但不能越权下载无关交付文件 | ✅ | `tests/test_files_api.py`、`tests/test_unit_permissions.py`（下载/文件引用权限）；`GET /members` 负载汇总透明字段 |

**结论：13 条标准全部满足，MVP 宣告完成（2026-07-29）。**

## 2. 性能基线（2026-07-28）

### 2.1 测量环境

| 项 | 值 |
| --- | --- |
| CPU | AMD Ryzen 7 8745HS（4 核可用） |
| 内存 | 3.8 GB |
| Docker | Server 29.3.1 / Compose 5.1.1 |
| 部署形态 | Docker Compose 六服务（postgres 16-alpine、redis 7-alpine、backend、worker、scheduler、frontend） |
| 测量方式 | 同网络容器内 httpx 直连 backend（`http://backend:8000`），脚本 `backend/scripts/benchmark.py` |
| 代表性数据量 | 10 名成员、101 个工作项（`perf_` 前缀种子，可复测累积）、数百条审计/通知 |

### 2.2 基线数据（2026-07-28 实测）

| 接口 | 样本 | 平均(ms) | p50(ms) | p95(ms) | 最大(ms) |
| --- | --- | --- | --- | --- | --- |
| POST /auth/login | 20 | 96.2 | 86.8 | 178.0 | 178.0 |
| GET /work-items（列表） | 30 | 17.7 | 16.1 | 28.1 | 28.2 |
| GET /members（负载汇总） | 30 | 25.9 | 25.5 | 31.4 | 33.4 |
| GET /approvals（审批聚合） | 30 | 8.9 | 8.8 | 10.2 | 11.4 |
| GET /notifications | 30 | 10.2 | 10.0 | 14.0 | 14.5 |
| POST /collaboration-requests（协作命令） | 15 | 34.1 | 30.4 | 67.3 | 67.3 |
| POST /work-items（创建+幂等键） | 15 | 24.7 | 24.5 | 28.3 | 28.3 |
| POST /files（上传 ~880KB） | 10 | 33.8 | 31.0 | 58.8 | 58.8 |
| GET /files/{id}/download | 10 | 23.3 | 23.8 | 29.6 | 29.6 |
| GET /events/stream（SSE 建立+首帧） | 10 | 12.5 | 12.2 | 15.9 | 15.9 |

### 2.3 结论与慢查询分析

- 登录耗时约 96ms，主要来自 Argon2 密码哈希（第 16 章要求的算法成本，属预期，非慢查询）。
- 全部读接口 p95 < 35ms，命令接口 p95 < 70ms，文件上传/下载（约 880KB）p95 < 60ms，SSE 建立 < 16ms。
- **未发现需要优化的慢查询**：101 工作项 + 负载汇总（`GROUP BY assignee_id`）下各列表接口均在 30ms 量级，既有索引（外键、状态列）已覆盖当前数据量；故本阶段未新增索引迁移。数据量增长一个数量级后应复测，首要关注 `GET /members` 的负载聚合与 `audit_events` 全表扫描类查询。

### 2.4 复测方法

```bash
docker compose run --rm --no-deps -v "$PWD/backend:/app" backend \
    python scripts/benchmark.py --base-url http://backend:8000
```

- 种子数据幂等：`perf_` 前缀成员与工作项已存在则复用，可用 `--items` 增大数据量复测。
- 成员登录密码与用户名相同（种子策略），仅供基线环境使用。

## 3. 备份恢复演练（2026-07-28）

- 演练日期：2026-07-28
- 执行人：Kimi Code CLI（自动化执行，人工任务下发）
- 环境：Docker Compose 全栈运行中（postgres 16-alpine / backend / worker / scheduler / redis / frontend 均 healthy）
- 使用脚本：`deploy/scripts/backup.sh`、`deploy/scripts/restore.sh`（用法见 `deploy/scripts/README.md`）

### 3.1 演练步骤

1. **造可识别业务数据**（不触碰现有数据，直接 SQL 插入）：
   - 用户 `restore_drill_user_20260728`、项目 `restore-drill-project-20260728`、
     成员（role=leader）、工作项 `恢复演练工作项-20260728`（status=IN_PROGRESS）；
   - 上传文件 `data/uploads/drill/drill_20260728.txt`（102 字节），
     `stored_files` 登记 `storage_key=drill/drill_20260728.txt`，
     `sha256=eba33f2cb6b2d78b88ad3a413afa705861eb331542e7c0e2825bf850d97b6eda`。
2. **执行备份**：`deploy/scripts/backup.sh`，退出码 0。
3. **恢复到全新库与全新目录**：
   ```bash
   deploy/scripts/restore.sh \
     --dump data/backups/postgres/20260728-183322.dump \
     --target-db agentos_restore_drill \
     --uploads-archive data/backups/uploads/20260728-183322-uploads.tar.gz \
     --uploads-target data/restore-drill/uploads
   ```
   退出码 0，全部自动校验通过。
4. **人工复核**：在恢复库中直接 SQL 查询业务数据；比对恢复文件哈希。
5. **演练后清理**：删除演练库 `agentos_restore_drill` 与目录 `data/restore-drill/`，
   保留备份产物（`data/backups/`）与本记录。

### 3.2 耗时

| 步骤 | 耗时 |
| --- | --- |
| 备份（pg_dump 112K + 上传目录 tar 4K） | < 1 秒 |
| 恢复（建库 + pg_restore + 解包 + 全部校验） | 约 2 秒 |

（数据量为 MVP 开发库量级，生产量级需重新计时。）

### 3.3 校验结果

#### 3.3.1 恢复脚本自动校验（输出摘要）

```text
[2026-07-28 18:33:49] 数据库恢复完成
[2026-07-28 18:33:49] 上传目录恢复完成，共 2 个文件
[2026-07-28 18:33:49] 校验通过：目标库可连接
[2026-07-28 18:33:50] 校验通过：核心表 users/projects/project_members/work_items/stored_files 均存在
[2026-07-28 18:33:50] stored_files 共 2 条记录，随机抽查最多 20 条
[2026-07-28 18:33:50] SHA-256 抽查结果：一致 2，不一致 0，文件缺失 0
[2026-07-28 18:33:50] 校验通过：抽查文件 SHA-256 全部与 stored_files 记录一致
[2026-07-28 18:33:50] ===== 恢复完成，全部校验通过 =====
```

#### 3.3.2 恢复库中业务数据查询（人工复核）

```text
          username           |            project             |          title          |   status
-----------------------------+--------------------------------+-------------------------+-------------
 restore_drill_user_20260728 | restore-drill-project-20260728 | 恢复演练工作项-20260728 | IN_PROGRESS
```

`stored_files` 两条记录（含演练前已有的 `t46.txt`）的 SHA-256 均与恢复目录中
对应文件实际哈希一致（抽查比例 2/2 = 100%）。

#### 3.3.3 安全保护验证

- 不带 `--confirm` 将 `--target-db` 指定为主库 `agentos`，脚本拒绝执行：
  `ERROR: 目标库是主库 agentos！覆盖主库属于危险操作，如确认无误请追加 --confirm`，退出码 1。

### 3.4 保留策略验证（同日完成）

- 用 `touch -d` 构造 mtime 为 15 天前、30 天前的假备份文件各 1 个，以及 13 天前的 1 个；
- 再次运行 `backup.sh`，日志显示：
  ```text
  已清理超期备份：/root/AgentOS/data/backups/postgres/20260713-020000.dump
  已清理超期备份：/root/AgentOS/data/backups/uploads/20260628-020000-uploads.tar.gz
  保留策略执行完毕，共清理 2 个超期文件
  ```
- 15 天、30 天前的假备份被删除，13 天前的保留，符合 14 天保留周期要求。

### 3.5 结论

备份 → 全新环境恢复 → 业务数据可查 → 文件 SHA-256 与 `stored_files` 记录一致的
完整链路验证通过，备份产物可用。**MVP 完成标准 12（恢复演练）达成。**

遗留说明：上传目录采用 `tar --listed-incremental` 增量备份，单包只含当次变更；
精确恢复到某一天需按时间顺序依次解包"全量基线 + 增量包"（详见
`deploy/scripts/README.md` 保留策略说明）。MVP 阶段上传体量极小，风险可接受。

## 4. 安全检查（2026-07-29）

核查方式：代码审查 + 数据库实测 + 未认证 HTTP 实测 + 端口监听实测。
结论：**全部通过，无需代码修复的未处理项**；1 项环境层面提醒（见 4.2）。

### 4.1 凭据与令牌

| 检查项 | 方法 | 结果 |
| --- | --- | --- |
| 密码 Argon2 哈希 | `psql` 抽查 `users.password_hash` | ✅ 全部 `$argon2id$v=19$m=65536,t=...`（Argon2id） |
| Refresh Token 哈希持久化 | `psql` 抽查 `refresh_tokens` | ✅ 只存 `token_hash`（20 字节十六进制前缀抽查），无明文 |
| Refresh Token 可撤销 | 表结构与 identity 代码 | ✅ `revoked_at` 字段实测有已撤销记录；logout/轮换路径存在 |
| 无公开注册入口 | grep 全部 router（register/signup）+ 实测 | ✅ 无注册端点；`POST /api/v1/members` 未认证实测 401（成员只能由负责人创建） |

### 4.2 权限

| 检查项 | 方法 | 结果 |
| --- | --- | --- |
| 全部 API 显式鉴权 | 逐域 router.py 核查 `Depends` 注入；未认证实测 | ✅ 13 个 router 全部走 `get_current_user`/`get_current_member`；`GET /work-items`、`GET /members`、`POST /members`、`GET /audit-events`、`GET /agent-suggestions` 未认证全部 401 |
| 项目角色与资源关系校验 | 抽查 service 层 + 测试覆盖 | ✅ `tests/test_unit_permissions.py`（14 例）、各域 API 测试覆盖非主执行人/无关成员/禁用成员分支 |
| 文件下载权限（T4.3） | 测试覆盖 | ✅ `tests/test_files_api.py`、协作回传文件引用的 uploader 校验（`test_unit_permissions.py`） |
| 透明范围（标准 13） | 代码 + 测试 | ✅ `member_to_out` 只序列化透明字段（无密码哈希/令牌）；成员可见全员负载/状态/摘要；交付文件正文、内部审核意见限相关成员（16 节） |

### 4.3 日志与数据外发

| 检查项 | 方法 | 结果 |
| --- | --- | --- |
| 日志不含密码/令牌/API Key/文件原文 | grep 全部 logger 调用 + 扫描 `data/logs/*.log` | ✅ 日志只记 user_id/username/request_id 等标识；日志文件内容扫描无 password/bearer/api_key 命中 |
| 模型只接收最小上下文 | agents 代码审查 | ✅ `agents/tools.py` 只读工具不给文件原文（`list_deliverable_metadata` 仅元数据）；`test_agent_guardrails.py` 覆盖 |
| 云端模型界面提示（16 节） | 前端代码 | ✅ `AgentAssistantPage` 与 `RequirementGuidedCreateDialog` 在 `llm_is_external=true` 时显示"数据将发送至外部服务"警示；`GET /config` 只暴露非敏感标识 |

### 4.4 网络面（19.4 节）

#### 4.4.1 端口监听实测（`ss -tlnp`）

| 端口 | 监听面 | 判定 |
| --- | --- | --- |
| 8000（backend，docker-proxy） | 0.0.0.0 | ✅ Web 入口，符合反向代理前端的预期 |
| 3000（frontend，docker-proxy） | 0.0.0.0 | ✅ Web 入口 |
| 5432（AgentOS compose postgres） | 未发布端口 | ✅ 仅 Compose 内网可达（compose 无 ports 映射） |
| 6379（AgentOS compose redis） | 未发布端口 | ✅ 同上 |
| 11434（Ollama） | 无监听 | ✅ 当前未运行，无暴露；启用后应确认仅监听宿主机本地/内网 |

#### 4.4.2 环境提醒（非 AgentOS 组件，无需本仓库修复）

- 宿主机**系统级** postgres（pid 877，非 AgentOS 容器）监听 `0.0.0.0:5432`，属环境既有服务
  （docker-compose.yml 注释中已说明宿主机 5432 被系统 postgres 占用）。建议系统管理员
  收紧其 `listen_addresses` 或防火墙规则；AgentOS 自身不使用该实例。
- 宿主机系统 redis 监听 `127.0.0.1:6379`（仅回环），无暴露风险。

### 4.5 结论

第 16 章与 19.4 节各项均有通过记录，未发现需要代码修复的问题（故无需回归测试）；
唯一提醒项为宿主机既有系统服务的监听面，已在 4.4.2 记录，不属于 AgentOS 代码范围。
