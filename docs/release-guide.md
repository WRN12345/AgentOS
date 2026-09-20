# AgentOS 发布指南

## 环境与配置

标准部署需要 Git、Docker Engine 或 Docker Desktop、Compose 插件和 Buildx 0.17 或更高版本。Linux 运维脚本使用 Bash 及常见 GNU 工具；Windows 建议在 WSL 中执行备份恢复。应用都在容器内运行时，无需宿主机 Python 或 Node.js。

获取代码后，在仓库根目录创建配置，已有 `.env` 时保留原文件：

```bash
cp -n .env.example .env
```

Windows PowerShell：

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

配置模板见 [.env.example](../.env.example)，容器实际传参见 [docker-compose.yml](../docker-compose.yml)。`.env` 供 Compose 变量替换使用，只有 Compose 声明的变量才会进入容器；应用支持某个配置项不代表仅编辑 `.env` 就会生效。不要提交真实凭据或打印完整配置。

| 配置组 | 发布要求 |
| --- | --- |
| `APP_ENV` | 生产使用 `production`，该标识不会自动完成安全加固 |
| `POSTGRES_*`、`DATABASE_URL` | 设置强数据库密码，同步连接串；密码特殊字符需 URL 编码；容器内地址为 `postgres:5432` |
| `REDIS_URL` | 容器内地址为 `redis:6379`，默认 DB 0 |
| `JWT_SECRET`、`BOOTSTRAP_ADMIN_*` | 首次启动前设置强随机签名密钥和管理员密码 |
| `ACCESS_TOKEN_EXPIRE_MINUTES`、`REFRESH_TOKEN_EXPIRE_DAYS` | 按会话安全策略设置有效期 |
| `LLM_PROVIDER`、`LLM_MODEL`、`OLLAMA_BASE_URL`、`OPENAI_COMPATIBLE_*` | 选择文本模型、服务地址及凭据；模型名不能留空才能正常分析 |
| `LLM_TIMEOUT_SECONDS`、`LLM_MAX_RETRIES`、`LLM_MAX_TOKENS` | 调用超时、重试和输出上限；推理模型的思考也可能占 token 额度 |
| `AGENT_RUN_MAX_RETRIES`、`AGENT_RUN_RETRY_BASE_SECONDS` | 运行级指数退避重试，与单次模型调用重试分开 |
| `EMBEDDING_*` | 独立配置向量模型；当前维度固定为 1024 |
| `STORAGE_BACKEND`、`STORAGE_ROOT`、`MINIO_*` | 新文件存储位置，历史文件仍需访问原后端 |
| `UPLOAD_*` | 上传大小及类型白名单，默认上限 20 MiB |

Compose 默认到期提醒扫描周期为 300 秒，风险扫描周期为 86400 秒。日志挂载到 `data/logs/`。默认项目由 bootstrap 创建，当前 Compose 没有透传 `BOOTSTRAP_PROJECT_NAME`；项目名称可通过管理功能维护。

## 网络安全

**仓库 Compose 不是生产安全模板。发布前必须核对实际端口绑定、云安全组与 Docker 转发防火墙规则，并配置 HTTPS。**

| 服务 | 当前宿主机端口 | 注意事项 |
| --- | --- | --- |
| Web | `3000:3000` | 未限制回环地址；生产通过受控反向代理访问 |
| API | `8000:8000` | 未限制回环地址；不能只保护 Web 而遗漏直连 API |
| PostgreSQL | `0.0.0.0:5436:5432` | 明确对所有 IPv4 地址发布；不需要远程数据库访问时移除映射或收紧到回环，必要时仅允许可信来源 |
| Redis | 未发布 | 仅 Compose 网络可达，宿主机不能直接连接该容器的 6379 |
| 可选 MinIO | `127.0.0.1:9000`、`127.0.0.1:9001` | API 与控制台仅宿主机回环；远程管理使用受控隧道 |

Ollama 不在 Compose 中。应用默认经 `host.docker.internal:11434` 连接宿主机，Linux 映射使用 `host-gateway`；Ollama 仅监听宿主机回环可能无法被容器访问，需允许 Docker 网络访问并阻止公网访问。不要直接暴露 `data/uploads/`。

## 启动与首次使用

从仓库根目录执行：

```bash
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 backend worker scheduler
```

默认启动 `frontend`、`backend`、`worker`、`scheduler`、`postgres`、`redis` 六个服务。backend 自动执行迁移和幂等 bootstrap；检查健康状态后仍需验证实际业务。

| 入口 | 默认地址 |
| --- | --- |
| Web | http://localhost:3000 |
| OpenAPI | http://localhost:8000/docs |
| API 健康检查 | http://localhost:8000/health |
| Web 代理健康检查 | http://localhost:3000/health |

1. 使用 `BOOTSTRAP_ADMIN_*` 创建的全局管理员登录管理控制台。
2. 创建业务账号，安全转交创建响应中仅展示一次的初始密码。
3. 为默认项目指定负责人，或创建项目并指定负责人。
4. 使用负责人账号添加项目成员并开展业务；普通账号可参加多个项目，在各项目拥有独立角色。

全局管理员由 `users.is_admin` 标识，bootstrap 不为其创建项目成员身份，也不会把它设为项目负责人。修改引导密码配置不会重置已有账号密码；目前没有用户自助改密入口，首次配置和凭据转交必须安全。

## 模型与存储

### 文本与向量模型

Ollama 部署需预先拉取所配置的文本模型，文档检索默认另需：

```bash
ollama pull qwen3-embedding:0.6b
```

文本模型和向量模型可以使用不同提供方。使用外部服务前确认允许发送业务上下文或文档片段；API Key 不应进入日志。模型故障会使相应 AI 功能失败或降级，人工业务操作仍可使用，但 `/health` 正常不能证明模型可用。

更换同为 1024 维的 embedding 模型时，在维护窗口先备份，统一更新 backend、worker、scheduler 配置并重建索引：

```bash
docker compose up -d backend worker scheduler
docker compose exec backend python -m app.scripts.rebuild_memory_index
# 确认预览后执行，清空旧向量并投递重建任务
docker compose exec backend python -m app.scripts.rebuild_memory_index --yes
```

Worker 必须运行且新模型可用；重建期间知识库不可用，命令退出不代表所有索引任务完成，应检查文件索引状态和 Worker 日志。维度变更需要专门数据库迁移，不能仅修改变量或运行重建脚本。

### 可选 MinIO

默认 `STORAGE_BACKEND=local`。使用内置实例时先设置强随机 `MINIO_ROOT_PASSWORD`，再执行：

```bash
docker compose --profile minio up -d minio
```

通过本机 9001 控制台创建私有 bucket 和受限应用账号，应用账号需要该 bucket 内对象读写删除权限，不使用管理账号。配置 `MINIO_ENDPOINT`（不带协议）、`MINIO_ACCESS_KEY`、`MINIO_SECRET_KEY`、`MINIO_BUCKET`、`MINIO_SECURE`，设置 `STORAGE_BACKEND=minio`，再运行 `docker compose up -d backend worker scheduler` 加载配置。外部 MinIO 无需启用 profile，生产连接使用 TLS。

历史文件仍按数据库记录访问原后端，保留本地卷与旧凭据直至迁移、备份和恢复验证完成。操作见[切换存储后端](#切换存储后端)和[备份与恢复](#备份与恢复)。

## 升级与恢复

1. 确认目标版本、迁移影响与维护窗口，保留可用的旧版本镜像或版本标识。
2. 在变更前执行 `deploy/scripts/backup.sh`，确认数据库 dump 和同批次完整文件快照均成功，并保存到独立故障域。
3. 升级代码后执行 `docker compose up -d --build`；后端公共代码变更涉及 backend、worker、scheduler，不能只重建其中一个。迁移期间避免旧业务进程继续写入。
4. 检查日志、健康状态和业务验证项，再恢复访问。自动迁移不是自动回滚机制，恢复旧镜像不保证兼容已升级数据库。

恢复操作与演练要求见下节。`docker compose down` 不删除绑定挂载的 `data/`，这些目录不是构建缓存。

## 备份与恢复

所有命令从仓库根目录执行。

| 脚本 | 作用 |
| --- | --- |
| `deploy/scripts/backup.sh` | PostgreSQL 逻辑备份、local/MinIO 完整文件快照和保留期清理 |
| `deploy/scripts/restore.sh` | 恢复数据库和存储快照，校验文件大小及 SHA-256；兼容历史本地归档 |

脚本通过 `docker compose exec postgres` 在容器内访问数据库，不依赖宿主机端口映射。文件工具通过一次性 backend 容器运行，执行前先构建与目标版本匹配的 backend 镜像。备份期间暂停存储迁移和文件删除；恢复期间停止业务写入及目标库连接。

脚本通过 shell 加载根目录 `.env`，只使用可信且兼容 shell 语法的配置文件；同名环境变量可能被文件中的赋值覆盖。未配置时使用脚本默认值。脚本的 `POSTGRES_*` 与容器 `DATABASE_URL` 必须指向同一数据库实例和库名，否则数据库 dump 与文件快照可能不匹配。

执行结果追加到 `data/logs/backup.log` 或 `data/logs/restore.log`，成功退出码为 0，失败非 0。`restore.sh --help` 输出帮助后退出码为 2，不代表已执行恢复。

### 备份

```bash
deploy/scripts/backup.sh
```

- 数据库产物：`data/backups/postgres/<时间戳>.dump`，使用 pg_dump 自定义格式，写出前通过 `pg_restore -l` 校验可读性。
- 文件产物：`data/backups/storage/<时间戳>-storage.tar.gz`，包含数据库登记的文件与需求素材、历史版本和所有后端；`manifest.json` 记录来源表、文件 ID、后端、key、大小及 SHA-256。
- 快照按数据库记录读取，不依赖当前 `STORAGE_BACKEND` 默认值，历史文件所在后端仍须可访问。
- 每批快照都是完整备份，须与同时间戳的数据库 dump 配对恢复；日志显示“备份完成”才代表整批成功。

数据库参数支持 `POSTGRES_SERVICE`、`POSTGRES_DB`、`POSTGRES_USER`、`POSTGRES_PASSWORD`。脚本另支持 `BACKUP_DIR`、`LOG_DIR`、`RETENTION_DAYS`；文件工具使用应用的 local/MinIO 配置。

### 恢复

恢复演练使用专用数据库、目录及私有 bucket。将示例中的文件路径替换为实际备份路径：

```bash
deploy/scripts/restore.sh \
  --dump data/backups/postgres/<时间戳>.dump \
  --target-db agentos_restore_drill \
  --storage-archive data/backups/storage/<时间戳>-storage.tar.gz \
  --uploads-target data/restore-drill/uploads
```

覆盖主库与线上上传目录是危险操作，必须显式确认：

```bash
deploy/scripts/restore.sh \
  --dump <数据库备份文件> --target-db agentos \
  --storage-archive <文件快照> --uploads-target data/uploads --confirm
```

- 目标数据库先删除再重建。自定义目标也可能已有数据，脚本不会自动识别其是否可丢弃，参数保护不能替代人工核对。
- 目标库名等于主库名、上传目标目录是 `data/uploads` 或目标 bucket 是当前配置的 bucket 时，必须加 `--confirm`。
- 含 MinIO 记录时必须显式传 `--minio-target-bucket agentos-restore-drill`，预先创建私有 bucket 并授权应用账号；连接地址与凭据来自环境变量。
- 重建数据库前先验证归档清单及内容。文件恢复保留记录中的后端和 key，逐文件校验大小及 SHA-256；目标已有相同内容时跳过，不同内容时拒绝覆盖，即使指定 `--confirm`。
- 缺失任何数据库引用文件时失败；允许快照包含 dump 之后新上传的额外文件，仅恢复数据库引用的记录。
- 仅恢复数据库可省略文件参数，但不等于文件已经恢复。
- 历史本地归档使用 `--uploads-archive ... --uploads-target ...`，只抽查 local 记录，默认 20 条（`VERIFY_SAMPLE_SIZE` 可调），不能用于恢复 MinIO 对象。

恢复不是跨数据库与对象存储的原子操作，中途失败可能已重建数据库或写入部分文件，须检查日志处理。演练恢复后，业务验证实例须指向目标库、上传目录和目标 bucket，不能沿用线上配置。

### 定时备份与保留策略

脚本不含定时逻辑。在宿主机执行 `crontab -e`，添加每日 02:30 的任务，路径按实际部署位置修改：

```cron
30 2 * * * cd /root/AgentOS && mkdir -p data/logs && ./deploy/scripts/backup.sh >> data/logs/backup-cron.log 2>&1
```

- cron 用户须有 Docker 权限，PATH 中须能找到 Docker 和所需工具；脚本自行解析仓库根目录并加载 `.env`。
- `RETENTION_DAYS` 默认 14，可在 `.env` 中设置，例如 `RETENTION_DAYS=30`；文件未设置时也可在命令前传入。
- 自动清理超过保留期的 `data/backups/postgres/*.dump` 和 `data/backups/storage/*-storage.tar.gz`，完整快照没有增量链依赖。
- 历史 `data/backups/uploads/` 归档不自动清理；旧增量备份恢复需要完整基线及后续增量包，不能只恢复最后一包。
- 每周检查 `backup.log` 的最近成功批次，每月至少执行一次独立目标恢复演练，记录版本、批次、目标、耗时、文件校验和业务复核结果。
- cron 标准输出写入独立的 `backup-cron.log`，避免重复写入脚本日志；日志轮转由宿主机运维负责。
- 备份包含私有项目文件，应限制目录权限并另存到独立故障域；同机快照不能抵御磁盘损坏。

## 切换存储后端

`STORAGE_BACKEND=local|minio` 只决定新文件写入位置。已有文件根据数据库记录读取，切换后保留旧后端配置及本地卷，直到迁移完成。

```bash
# 预览，不改变文件或数据库
docker compose run --rm --no-deps backend python -m app.scripts.migrate_file_storage \
  --from-backend local --to-backend minio

# 复制、校验，再更新数据库后端标记；保留源文件
docker compose run --rm --no-deps backend python -m app.scripts.migrate_file_storage \
  --from-backend local --to-backend minio --yes
```

迁移覆盖 `stored_files` 和 `project_materials`，保留文件 ID、key 和全部版本，无需重建记忆向量。需求素材表没有持久化 SHA-256，工具读取源内容计算哈希并校验目标；常规文件另外与数据库已有哈希校验。可重复执行，目标同 key 内容不匹配时失败而不覆盖。备份和迁移分开安排维护窗口，源文件只在备份与恢复验证完成后人工清理。

## 发布验证

- 六个默认服务健康，Web 可打开，API 与代理 `/health` 返回 200；MinIO 启用时另验连接和对象读写。
- 管理员能管理账号与项目，普通用户可选择授权项目；携带其他项目上下文不能读取或修改其业务数据。
- 负责人创建工作项，成员提交开发文档，经确认或豁免后开工，提交交付并完成审核；检查审计和通知。
- 上传、下载和历史版本可用；使用 MinIO 时同时检查仍留在 local 的历史文件。
- 已配置的 Agent 和文档检索成功；模型故障时人工工作流可继续。健康探针不覆盖这一步。
- 检查端口实际暴露面、HTTPS、凭据保管及日志脱敏，确认异地备份和恢复演练可用。

## 开发与故障排查

开发和测试命令见 [README](../README.md#开发与测试)。后端测试会创建、迁移并清空专用 `_test` 数据库，Redis 默认使用 DB 15；只在隔离的开发或测试环境执行，不要连接生产数据库或共享队列。

宿主机运行后端需 Python 3.12+，前端建议 Node.js 20+。连接仓库 PostgreSQL 应使用 `localhost:5436`，不是 5432；Redis 默认没有宿主机端口，须提供独立开发实例或仅绑定回环的开发映射。API、Worker 和 Scheduler 使用一致的数据库、Redis、存储与模型配置，启动 API 前执行迁移和 bootstrap。推荐直接在 Compose 网络内开发测试，避免混用宿主机服务。

### 本地数据路径

本地源码运行默认将日志和上传保存在仓库根 `data/logs/`、`data/uploads/`。`LOG_DIR`、`STORAGE_ROOT` 的相对值以应用项目根解析，不随启动工作目录变化；显式绝对路径保持不变。

源码根通过 `backend/pyproject.toml` 和仓库 `docker-compose.yml` 识别；容器源码的项目根为 `/app`。脱离源码树的已安装包须显式配置绝对 `LOG_DIR`、`STORAGE_ROOT`，不向虚拟环境写入数据。

`.env.example` 用于容器：镜像与 Compose 保持 `/app/data/logs`、`/app/data/uploads`，挂载对应宿主机根 `data/`。本地启动若加载了该示例配置，可用进程环境 `LOG_DIR=data/logs STORAGE_ROOT=data/uploads` 覆盖。宿主机备份/恢复脚本的 `LOG_DIR` 应使用宿主机路径，而不是容器路径。

测试在导入 app 前将日志和默认本地存储隔离到会话临时目录，文件用例使用 `tmp_path`。以下路径和独立存储测试覆盖了数据库 fixture，无需启动服务，从仓库根目录执行：

```bash
PYTHONPATH=backend STORAGE_BACKEND=local python -m pytest -q \
  backend/tests/test_data_paths.py backend/tests/test_storage_provider.py \
  backend/tests/test_storage_minio.py backend/tests/test_storage_tools.py
```

### 常见问题

| 现象 | 核查方向 |
| --- | --- |
| backend unhealthy | 迁移和 bootstrap 日志、数据库/Redis 可达性；不要靠反复清空数据目录解决 |
| Agent failed | 模型名、地址、已拉取模型、凭据、超时和结构化输出；`echo` 仅验证基础管道 |
| 文件索引失败或卡住 | Worker、embedding 维度与服务、源文件后端、索引租约恢复日志 |
| 项目接口 400/403/404 | `X-Project-Id`、有效成员身份、对象归属；全局管理员不是业务成员 |
| 配置未生效 | Compose 是否透传该变量、容器是否已重建；`restart` 不加载新的容器环境 |
| 恢复失败 | 备份是否配对、目标库连接是否停止、MinIO bucket/授权及已有同 key 文件是否冲突 |
