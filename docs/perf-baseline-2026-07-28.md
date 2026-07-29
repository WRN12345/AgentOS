# 性能基线报告 2026-07-28（T6.7）

## 1. 测量环境

| 项 | 值 |
| --- | --- |
| CPU | AMD Ryzen 7 8745HS（4 核可用） |
| 内存 | 3.8 GB |
| Docker | Server 29.3.1 / Compose 5.1.1 |
| 部署形态 | Docker Compose 六服务（postgres 16-alpine、redis 7-alpine、backend、worker、scheduler、frontend） |
| 测量方式 | 同网络容器内 httpx 直连 backend（`http://backend:8000`），脚本 `backend/scripts/benchmark.py` |
| 代表性数据量 | 10 名成员、101 个工作项（`perf_` 前缀种子，可复测累积）、数百条审计/通知 |

## 2. 基线数据（2026-07-28 实测）

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

## 3. 结论与慢查询分析

- 登录耗时约 96ms，主要来自 Argon2 密码哈希（第 16 章要求的算法成本，属预期，非慢查询）。
- 全部读接口 p95 < 35ms，命令接口 p95 < 70ms，文件上传/下载（约 880KB）p95 < 60ms，SSE 建立 < 16ms。
- **未发现需要优化的慢查询**：101 工作项 + 负载汇总（`GROUP BY assignee_id`）下各列表接口均在 30ms 量级，既有索引（外键、状态列）已覆盖当前数据量；故本阶段未新增索引迁移。数据量增长一个数量级后应复测，首要关注 `GET /members` 的负载聚合与 `audit_events` 全表扫描类查询。

## 4. 复测方法

```bash
docker compose run --rm --no-deps -v "$PWD/backend:/app" backend \
    python scripts/benchmark.py --base-url http://backend:8000
```

- 种子数据幂等：`perf_` 前缀成员与工作项已存在则复用，可用 `--items` 增大数据量复测。
- 成员登录密码与用户名相同（种子策略），仅供基线环境使用。
