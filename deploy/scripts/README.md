AgentOS 备份与恢复脚本

对应设计文档 `docs/2026-07-26-agentos-workflow-platform-design.md` 19.4 节与任务 T6.5。

## 脚本一览

| 脚本 | 作用 |
| --- | --- |
| `backup.sh` | PostgreSQL 逻辑备份 + local/MinIO 完整文件快照 + 14 天保留清理 |
| `restore.sh` | 恢复数据库和存储快照，校验全部文件大小与 SHA-256；兼容历史本地归档 |

两个脚本都：

- 从仓库根目录的 `.env`（若存在）读取配置，否则使用与 `docker-compose.yml` 一致的默认值；
- 通过 `docker compose exec postgres` 在 Compose 网络内访问数据库，**不需要发布 5432 端口**；
- 结果追加写入 `data/logs/backup.log` / `data/logs/restore.log`，成功退出码 0，失败非 0。
- 文件工具通过一次性 backend 容器运行，发布后先构建 backend 镜像。备份期间暂停存储迁移和文件删除；恢复期间停止业务写入。

## 备份

```bash
deploy/scripts/backup.sh
```

产物：

- `data/backups/postgres/<时间戳>.dump` — pg_dump 自定义格式逻辑备份，写出前会用 `pg_restore -l` 校验可读；
- `data/backups/storage/<时间戳>-storage.tar.gz` — 数据库登记的文件与需求素材，包含历史版本和所有后端；归档内的 `manifest.json` 记录来源表、文件 ID、后端、key、大小与 SHA-256；
- 快照按数据库记录读取，不依赖 `STORAGE_BACKEND` 当前默认值；历史文件所在后端仍须可访问；
- 每批文件快照都是完整备份，须与同时间戳的数据库 dump 配对恢复；日志只有显示“备份完成”才代表整批成功；
- 超过 14 天（`RETENTION_DAYS` 可调）的 `.dump` 与 `*-storage.tar.gz` 自动删除。

可配置环境变量（默认值即 docker-compose.yml 默认值）：`POSTGRES_DB`、`POSTGRES_USER`、
`POSTGRES_PASSWORD`、`BACKUP_DIR`、`LOG_DIR`、`RETENTION_DAYS`，以及应用的 local/MinIO 配置。

## 恢复

```bash
# 恢复到全新库与全新目录（恢复演练 / 验证备份可用性）
deploy/scripts/restore.sh \
  --dump data/backups/postgres/<时间戳>.dump \
  --target-db agentos_restore_drill \
  --storage-archive data/backups/storage/<时间戳>-storage.tar.gz \
  --uploads-target data/restore-drill/uploads

# 恢复覆盖主库与线上上传目录（危险操作，必须显式 --confirm）
deploy/scripts/restore.sh \
  --dump <备份文件> --target-db agentos \
  --storage-archive <备份文件> --uploads-target data/uploads --confirm
```

行为与保护：

- 目标库先 `DROP DATABASE IF EXISTS` 再重建，保证恢复结果只来自备份；
- 目标库名等于主库名、上传目标目录是 `data/uploads` 或目标 bucket 是当前配置的 bucket 时，必须加 `--confirm`；
- 含 MinIO 记录时必须显式传 `--minio-target-bucket agentos-restore-drill`，并预先创建私有 bucket、给应用账号授权；连接地址与凭据来自环境变量；
- 重建目标数据库前先验证归档清单及文件内容；文件恢复保留记录中的后端和 key，逐文件校验大小及 SHA-256。目标已有相同内容时跳过，已有不同内容时拒绝覆盖，即使指定 `--confirm`；
- 快照缺失任何数据库引用的文件时失败；允许快照含有 dump 之后新上传的额外文件，仅恢复数据库引用的记录；
- 只恢复数据库可省略文件参数；这不等于文件已恢复；
- 历史本地归档仍使用 `--uploads-archive ... --uploads-target ...`，只抽查 `storage_backend=local` 的记录，默认 20 条（`VERIFY_SAMPLE_SIZE` 可调），不能用它恢复 MinIO 对象。

## 切换存储后端

`STORAGE_BACKEND=local|minio` 只决定新文件写入位置。已有文件根据 `stored_files.storage_backend` 读取，切换后保留旧后端配置及本地卷，直到迁移完成。

```bash
# 只预览，不改变文件或数据库
docker compose run --rm --no-deps backend python -m app.scripts.migrate_file_storage \
  --from-backend local --to-backend minio

# 逐文件复制、校验，再更新数据库后端标记；保留原始本地文件
docker compose run --rm --no-deps backend python -m app.scripts.migrate_file_storage \
  --from-backend local --to-backend minio --yes
```

迁移覆盖 `stored_files` 和 `project_materials`，保留文件 ID、key 和全部版本，不需要重建记忆向量。需求素材表没有持久化 SHA-256，工具读取源内容计算哈希，再校验目标；常规文件另外与数据库已有哈希校验。可重复执行；目标同 key 内容不匹配时报告失败，不覆盖。备份与迁移应分开安排维护窗口，迁移源文件只在备份和恢复验证完成后人工清理。

## 宿主机 crontab 配置（每日定时备份）

脚本本身不含定时逻辑，由宿主机 cron 触发。配置方法：

```bash
crontab -e
```

加入一行（每天 02:30 执行，输出并入备份日志；路径按实际部署位置修改）：

```cron
30 2 * * * cd /root/AgentOS && ./deploy/scripts/backup.sh >> data/logs/backup.log 2>&1
```

说明：

- cron 环境极简，脚本内部已自行解析仓库根目录并加载 `.env`，无需额外环境变量；
- 如需调整保留周期，可在 crontab 行内指定：`RETENTION_DAYS=30 ./deploy/scripts/backup.sh`；
- 建议每周检查一次 `data/logs/backup.log` 尾部，确认最近批次为 `备份完成`；
- 按 19.4 节要求，每月至少执行一次恢复演练（用上面"恢复到全新库"的用法），
  演练记录存档到 `docs/restore-drill-<日期>.md`。

## 保留策略说明

- 清理 `data/backups/postgres/*.dump` 与 `data/backups/storage/*-storage.tar.gz` 中超过 `RETENTION_DAYS`（默认 14）天的文件；完整快照没有增量链依赖。
- 历史 `data/backups/uploads/` 归档不自动清理；旧增量备份恢复需要完整基线及后续增量包，不能只恢复最后一包。
- 备份含私有项目文件，应限制备份目录权限，并另存到独立故障域；同机快照不能抵御磁盘损坏。
