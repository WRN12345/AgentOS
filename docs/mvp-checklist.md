# MVP 完成标准核对清单（设计文档第 22 章）

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
| 11 | Docker Compose 能在 Debian 上启动全部应用服务 | ✅ | `docs/release-guide.md` 第 9 节部署验证记录（build → down/up → 六服务 healthy → 登录实测） |
| 12 | 数据库和文件备份能完成一次实际恢复 | ✅ | `docs/restore-drill-2026-07-28.md`（恢复到全新库+目录，SHA-256 抽查 2/2 一致） |
| 13 | 成员能看全员工作量/状态/摘要，但不能越权下载无关交付文件 | ✅ | `tests/test_files_api.py`、`tests/test_unit_permissions.py`（下载/文件引用权限）；`GET /members` 负载汇总透明字段 |

**结论：13 条标准全部满足，MVP 宣告完成（2026-07-29）。**
