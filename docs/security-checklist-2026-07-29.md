# 安全检查记录 2026-07-29（T6.6，第 16 章 + 19.4 节）

核查方式：代码审查 + 数据库实测 + 未认证 HTTP 实测 + 端口监听实测。
结论：**全部通过，无需代码修复的未处理项**；1 项环境层面提醒（见 4.2）。

## 1. 凭据与令牌

| 检查项 | 方法 | 结果 |
| --- | --- | --- |
| 密码 Argon2 哈希 | `psql` 抽查 `users.password_hash` | ✅ 全部 `$argon2id$v=19$m=65536,t=...`（Argon2id） |
| Refresh Token 哈希持久化 | `psql` 抽查 `refresh_tokens` | ✅ 只存 `token_hash`（20 字节十六进制前缀抽查），无明文 |
| Refresh Token 可撤销 | 表结构与 identity 代码 | ✅ `revoked_at` 字段实测有已撤销记录；logout/轮换路径存在 |
| 无公开注册入口 | grep 全部 router（register/signup）+ 实测 | ✅ 无注册端点；`POST /api/v1/members` 未认证实测 401（成员只能由负责人创建） |

## 2. 权限

| 检查项 | 方法 | 结果 |
| --- | --- | --- |
| 全部 API 显式鉴权 | 逐域 router.py 核查 `Depends` 注入；未认证实测 | ✅ 13 个 router 全部走 `get_current_user`/`get_current_member`；`GET /work-items`、`GET /members`、`POST /members`、`GET /audit-events`、`GET /agent-suggestions` 未认证全部 401 |
| 项目角色与资源关系校验 | 抽查 service 层 + 测试覆盖 | ✅ `tests/test_unit_permissions.py`（14 例）、各域 API 测试覆盖非主执行人/无关成员/禁用成员分支 |
| 文件下载权限（T4.3） | 测试覆盖 | ✅ `tests/test_files_api.py`、协作回传文件引用的 uploader 校验（`test_unit_permissions.py`） |
| 透明范围（标准 13） | 代码 + 测试 | ✅ `member_to_out` 只序列化透明字段（无密码哈希/令牌）；成员可见全员负载/状态/摘要；交付文件正文、内部审核意见限相关成员（16 节） |

## 3. 日志与数据外发

| 检查项 | 方法 | 结果 |
| --- | --- | --- |
| 日志不含密码/令牌/API Key/文件原文 | grep 全部 logger 调用 + 扫描 `data/logs/*.log` | ✅ 日志只记 user_id/username/request_id 等标识；日志文件内容扫描无 password/bearer/api_key 命中 |
| 模型只接收最小上下文 | agents 代码审查 | ✅ `agents/tools.py` 只读工具不给文件原文（`list_deliverable_metadata` 仅元数据）；`test_agent_guardrails.py` 覆盖 |
| 云端模型界面提示（16 节） | 前端代码 | ✅ `AgentAssistantPage` 与 `RequirementGuidedCreateDialog` 在 `llm_is_external=true` 时显示"数据将发送至外部服务"警示；`GET /config` 只暴露非敏感标识 |

## 4. 网络面（19.4 节）

### 4.1 端口监听实测（`ss -tlnp`）

| 端口 | 监听面 | 判定 |
| --- | --- | --- |
| 8000（backend，docker-proxy） | 0.0.0.0 | ✅ Web 入口，符合反向代理前端的预期 |
| 3000（frontend，docker-proxy） | 0.0.0.0 | ✅ Web 入口 |
| 5432（AgentOS compose postgres） | 未发布端口 | ✅ 仅 Compose 内网可达（compose 无 ports 映射） |
| 6379（AgentOS compose redis） | 未发布端口 | ✅ 同上 |
| 11434（Ollama） | 无监听 | ✅ 当前未运行，无暴露；启用后应确认仅监听宿主机本地/内网 |

### 4.2 环境提醒（非 AgentOS 组件，无需本仓库修复）

- 宿主机**系统级** postgres（pid 877，非 AgentOS 容器）监听 `0.0.0.0:5432`，属环境既有服务
  （docker-compose.yml 注释中已说明宿主机 5432 被系统 postgres 占用）。建议系统管理员
  收紧其 `listen_addresses` 或防火墙规则；AgentOS 自身不使用该实例。
- 宿主机系统 redis 监听 `127.0.0.1:6379`（仅回环），无暴露风险。

## 5. 结论

第 16 章与 19.4 节各项均有通过记录，未发现需要代码修复的问题（故无需回归测试）；
唯一提醒项为宿主机既有系统服务的监听面，已在 4.2 记录，不属于 AgentOS 代码范围。
