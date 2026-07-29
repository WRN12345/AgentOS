# 恢复演练记录 2026-07-28（T6.5 / MVP 标准 12 证据）

- 演练日期：2026-07-28
- 执行人：Kimi Code CLI（自动化执行，人工任务下发）
- 环境：Docker Compose 全栈运行中（postgres 16-alpine / backend / worker / scheduler / redis / frontend 均 healthy）
- 使用脚本：`deploy/scripts/backup.sh`、`deploy/scripts/restore.sh`（用法见 `deploy/scripts/README.md`）

## 1. 演练步骤

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

## 2. 耗时

| 步骤 | 耗时 |
| --- | --- |
| 备份（pg_dump 112K + 上传目录 tar 4K） | < 1 秒 |
| 恢复（建库 + pg_restore + 解包 + 全部校验） | 约 2 秒 |

（数据量为 MVP 开发库量级，生产量级需重新计时。）

## 3. 校验结果

### 3.1 恢复脚本自动校验（输出摘要）

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

### 3.2 恢复库中业务数据查询（人工复核）

```text
          username           |            project             |          title          |   status
-----------------------------+--------------------------------+-------------------------+-------------
 restore_drill_user_20260728 | restore-drill-project-20260728 | 恢复演练工作项-20260728 | IN_PROGRESS
```

`stored_files` 两条记录（含演练前已有的 `t46.txt`）的 SHA-256 均与恢复目录中
对应文件实际哈希一致（抽查比例 2/2 = 100%）。

### 3.3 安全保护验证

- 不带 `--confirm` 将 `--target-db` 指定为主库 `agentos`，脚本拒绝执行：
  `ERROR: 目标库是主库 agentos！覆盖主库属于危险操作，如确认无误请追加 --confirm`，退出码 1。

## 4. 保留策略验证（同日完成）

- 用 `touch -d` 构造 mtime 为 15 天前、30 天前的假备份文件各 1 个，以及 13 天前的 1 个；
- 再次运行 `backup.sh`，日志显示：
  ```text
  已清理超期备份：/root/AgentOS/data/backups/postgres/20260713-020000.dump
  已清理超期备份：/root/AgentOS/data/backups/uploads/20260628-020000-uploads.tar.gz
  保留策略执行完毕，共清理 2 个超期文件
  ```
- 15 天、30 天前的假备份被删除，13 天前的保留，符合 14 天保留周期要求。

## 5. 结论

备份 → 全新环境恢复 → 业务数据可查 → 文件 SHA-256 与 `stored_files` 记录一致的
完整链路验证通过，备份产物可用。**MVP 完成标准 12（恢复演练）达成。**

遗留说明：上传目录采用 `tar --listed-incremental` 增量备份，单包只含当次变更；
精确恢复到某一天需按时间顺序依次解包"全量基线 + 增量包"（详见
`deploy/scripts/README.md` 保留策略说明）。MVP 阶段上传体量极小，风险可接受。
