AgentOS 备份与恢复脚本

对应设计文档 `docs/2026-07-26-agentos-workflow-platform-design.md` 19.4 节与任务 T6.5。

## 脚本一览

| 脚本 | 作用 |
| --- | --- |
| `backup.sh` | 每日备份：PostgreSQL 逻辑备份 + 上传目录增量备份 + 14 天保留清理 |
| `restore.sh` | 从备份恢复数据库与上传目录，并做恢复后校验（含 SHA-256 抽查） |

两个脚本都：

- 从仓库根目录的 `.env`（若存在）读取配置，否则使用与 `docker-compose.yml` 一致的默认值；
- 通过 `docker compose exec postgres` 在 Compose 网络内访问数据库，**不需要发布 5432 端口**；
- 结果追加写入 `data/logs/backup.log` / `data/logs/restore.log`，成功退出码 0，失败非 0。

## 备份

```bash
deploy/scripts/backup.sh
```

产物：

- `data/backups/postgres/<时间戳>.dump` — pg_dump 自定义格式逻辑备份，写出前会用 `pg_restore -l` 校验可读；
- `data/backups/uploads/<时间戳>-uploads.tar.gz` — 上传目录 `tar --listed-incremental` 增量备份
  （快照文件为 `data/backups/uploads/.uploads.snar`，删除后下一次备份即回到全量基线）；
- 超过 14 天（`RETENTION_DAYS` 可调）的 `.dump` 与 `*-uploads.tar.gz` 自动删除。

可配置环境变量（默认值即 docker-compose.yml 默认值）：`POSTGRES_DB`、`POSTGRES_USER`、
`POSTGRES_PASSWORD`、`UPLOADS_DIR`、`BACKUP_DIR`、`LOG_DIR`、`RETENTION_DAYS`。

## 恢复

```bash
# 恢复到全新库与全新目录（恢复演练 / 验证备份可用性）
deploy/scripts/restore.sh \
  --dump data/backups/postgres/<时间戳>.dump \
  --target-db agentos_restore_drill \
  --uploads-archive data/backups/uploads/<时间戳>-uploads.tar.gz \
  --uploads-target data/restore-drill/uploads

# 恢复覆盖主库与线上上传目录（危险操作，必须显式 --confirm）
deploy/scripts/restore.sh \
  --dump <备份文件> --target-db agentos \
  --uploads-archive <备份文件> --uploads-target data/uploads --confirm
```

行为与保护：

- 目标库先 `DROP DATABASE IF EXISTS` 再重建，保证恢复结果只来自备份；
- 目标库名等于主库名、或上传目标目录是 `data/uploads` 时，必须加 `--confirm`，否则拒绝执行；
- 恢复后自动校验：目标库可连接 → 核心表（users/projects/project_members/work_items/stored_files）
  存在 → 随机抽查 `stored_files` 记录（默认 20 条，`VERIFY_SAMPLE_SIZE` 可调），
  比对恢复目录中文件的 SHA-256 与记录值，全部一致才算成功；
- 只恢复数据库可不传 `--uploads-archive/--uploads-target`，二者必须同时提供或同时省略。

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

- 仅清理 `data/backups/postgres/*.dump` 与 `data/backups/uploads/*-uploads.tar.gz` 中
  mtime 超过 `RETENTION_DAYS`（默认 14）天的文件；
- 增量快照文件 `.uploads.snar` 不在清理范围内，增量链不会被误删；
- 注意：`--listed-incremental` 的单个增量包只含当次变更文件，恢复某一天的完整上传目录
  需要"全量基线 + 其后的增量包"依次解包。本仓库上传目录体量小（MVP 阶段），
  恢复演练默认使用最近的全量/增量包直接恢复；若需精确到某天，按时间顺序解包多个 tar 即可。
