# 阶段 4 开发者指南：交付与审核

本文面向刚加入 AgentOS 的开发者，说明阶段 4（T4.1–T4.6）实现的**实际形态与设计理由**。文中章节号指设计文档 `docs/2026-07-26-agentos-workflow-platform-design.md`。阶段 1–3 的基线与机制（错误格式、幂等、审计、认证、工作项、协作、SSE、shadcn/ui 约定）见 `docs/phase-1/2/3-developer-guide.md`，本文不重复。

阶段 4 交付内容：

- 存储抽象 `StorageProvider` 与 `LocalStorageProvider`（T4.1，第 14 章）。
- 文件上传 `POST /files` 与 `stored_files` 落库（T4.2，第 11、12.5、14 章）。
- 文件下载 `GET /files/{id}/download` 与权限校验（T4.3，第 16 章）。
- 三类交付物（Git 链接/文本/文件）的版本化提交（T4.4，7.5 节）。
- 负责人最终审核与 `reviews` 留痕，闭环 8.1 节 `IN_REVIEW` 分支（T4.5，7.5 节）。
- 前端提交交付、版本历史、文件上传进度与审批中心"交付审核"区（T4.6，13.1/13.2 节）。

验证基线：`docker compose exec backend pytest` → **205 passed**；`npm run build`（tsc strict）零错误；六服务全部 healthy。

## 1. 存储抽象（T4.1，第 14 章）

代码位置：`backend/app/infrastructure/storage/`（与 queue/events 同级的技术机制，不放领域内）。

- `provider.py`：`StorageProvider` 抽象接口——`save/load/delete/exists/iter_chunks` 最小集合 + `stage/commit/discard` 暂存流程（`StagedUpload` 写入器）。暂存区与正式目录同文件系统，保证 `os.replace` 原子落位。`get_storage_provider()` 单例工厂作为 FastAPI 依赖项，测试用 `dependency_overrides` 注入。`S3StorageProvider` 不实现，接口 docstring 说明演进空间（多后端由 `stored_files.storage_backend` 列承载）。
- `local.py`：`LocalStorageProvider` 写入配置根目录；`_validate_key` 拒绝绝对路径与 `..`，数据库只保存相对 `storage_key`，不落宿主机绝对路径。
- 配置项进 `core/config.py`（compose 与 `.env.example` 已接）：`STORAGE_BACKEND=local`、`STORAGE_ROOT=/app/data/uploads`、`UPLOAD_MAX_BYTES=20971520`（20MB）、`UPLOAD_ALLOWED_EXTENSIONS`（.txt,.md,.csv,.json,.pdf,.png,.jpg,.jpeg,.zip）、`UPLOAD_ALLOWED_MIME_TYPES`（对应逗号列表，config 以 property 解析为集合）。
- **禁止直接暴露上传目录**：`frontend/nginx.conf` 无 `data/uploads` 映射（已核查），API 是唯一入口。
- 业务层只依赖接口：grep 验证 `app/domains/`、`app/api/` 无文件系统路径 / `LocalStorageProvider` / `os.replace` 引用。

## 2. 文件上传（T4.2，12.5、14 章）

代码位置：`backend/app/domains/files/`（五件套）。

### 2.1 stored_files 表（迁移 0007）

`id` / `storage_backend` / `storage_key`（相对键，唯一）/ `original_filename` / `size_bytes` / `mime_type` / `sha256` / `uploaded_by`(FK project_members) / `work_item_id`(FK work_items，**可空**，交付物关联时回填) / 时间戳。响应**不含** `storage_key`（最小暴露）。

### 2.2 POST /api/v1/files

multipart（字段 `file` + 可选 form 字段 `work_item_id`），要求登录态，支持 `Idempotency-Key`。严格按 14 章流程：

1. 流式写入 `.tmp/` 暂存文件（不落内存）；
2. 校验大小（413 `FILE_TOO_LARGE`）、扩展名与 MIME（415 `FILE_TYPE_NOT_ALLOWED`）；
3. 边读边算 SHA-256；
4. `os.replace` 原子移动到正式目录（按哈希前两位分桶 `63/63ed…_<rand>`）；
5. 同事务写 `stored_files` + 审计（`file.uploaded`）。

**补偿清理**（17.2 节）：落库失败时删除已落盘文件，磁盘无残留（有测试模拟 DB 失败断言）。依赖 `python-multipart==0.0.20`（requirements/pyproject 已加）。

### 2.3 可复用权限辅助

`domains/files/service.py` 的 `is_work_item_related(session, work_item_id, member_id)`（主执行人/协作者/协作请求双方）与 `can_download_file(...)`——deliverables 查询、reviews 可见性、协作回传文件校验全部复用这两个函数，不要另写一套。

## 3. 文件下载（T4.3，第 16 章）

`GET /api/v1/files/{id}/download`：FastAPI 鉴权后权限校验，后端 `StreamingResponse` 流式返回，不暴露上传目录。

- 权限规则（16 节）：与文件关联工作项有关的人（主执行人/协作者/协作请求双方）+ 项目负责人可下；无关成员 403 `FORBIDDEN`。
- 响应头：`content-type` 取库中 MIME；`content-disposition: attachment; filename="..."; filename*=UTF-8''...`（中文名 RFC 5987 编码）。
- 记录不存在或物理文件已清理 → 404 统一错误格式。
- 成功下载写 `file.downloaded` 审计（actor/target/after；request_id/source_ip 由 contextvars 自动带），支撑第 22 章标准 13。

## 4. 交付物版本化（T4.4，7.5 节）

代码位置：`backend/app/domains/deliverables/`（五件套）。

### 4.1 deliverables 表（迁移 0008）

`id` / `work_item_id`(FK) / `type`（`git_link|text|file`，CHECK）/ `content`（Text 可空，git 链接或文本正文）/ `stored_file_id`(FK stored_files 可空) / `version` / `submitted_by`(FK) / 时间戳；**`UNIQUE(work_item_id, version)`** 兜底并发重提。

### 4.2 接口与规则

- `POST /work-items/{id}/deliverables`（201，Idempotency-Key）：入参 `{type, content?, file_id?}`，pydantic 校验组合（git_link/text 必须 content，file 必须 file_id，否则 422）。**仅当前主执行人**；版本 = 该工作项 max(version)+1，唯一约束冲突 → 409 `DELIVERABLE_VERSION_CONFLICT`；终态（COMPLETED/CANCELLED）拒新交付物（409）。file 类型调用 `validate_file_reference`：文件存在、归属同一工作项（`stored_files.work_item_id` 为空则同事务回填）、上传人与工作项有关；响应内嵌 `file:{id, original_filename, size_bytes, mime_type, sha256}`，哈希可追溯（2.1 节）。
- 查询：`GET /work-items/{id}/deliverables`（版本倒序历史）与 `GET .../deliverables/{version}`；负责人 + 工作项相关成员可见（复用 `is_work_item_related`），无关成员 403。
- **submit 打通**：`work_items/service.py` 的 `run_command()` 在 `submit` 时直接查 Deliverable 模型（避免循环导入），无交付物 → 422 `DELIVERABLE_REQUIRED` 提示先提交交付物。全仓库仅 `test_work_items_api.py` 一处既有 submit 流程测试受影响，在该测试 unblock 后补发了一条 text 交付物，其余测试零改动。
- 审计 `deliverable.submitted`。**deliverable.\*/file.\* 只是审计 action，后端不发布 SSE**（无 notify 调用）——前端不监听这些类型，见 6.3 节。

### 4.3 协作回传引用交付物

`collaboration_requests` 新增可空列 `result_deliverable_id` / `result_file_id`（迁移 0008）。`POST /collaboration-requests/{id}/submit` 可携带二者：校验交付物属本工作项（否则 422）、文件校验与 file 类交付物共用 `validate_file_reference`（未关联则回填 `work_item_id`）。`result_text` 文本回传维持不变。

## 5. 最终审核（T4.5，7.5、8.1 节）

代码位置：`backend/app/domains/reviews/`（五件套）。

### 5.1 reviews 表（迁移 0008）

`id` / `work_item_id`(FK) / `deliverable_id`(FK，被审的交付物版本) / `decision`（`approve|request_changes|reject`，CHECK）/ `feedback`（可空）/ `reviewed_by`(FK) / 时间戳。

### 5.2 POST /work-items/{id}/reviews

- **仅项目负责人**（普通成员 403）；工作项须处于 `IN_REVIEW`（否则 409）；支持 `Idempotency-Key`。
- 三种结论（7.5 节），走既有状态机迁移：
  - `approve` → `IN_REVIEW → COMPLETED`（`complete`）；
  - `request_changes` → `IN_REVIEW → IN_PROGRESS`（必须填 feedback，422 校验）；
  - `reject` → 保持 `IN_REVIEW`（"拒绝当前交付但保持工作项继续执行"）。
- 同事务写入：reviews 记录 + `work_items` 状态/version+1 + 审计（`review.approved|changes_requested|rejected`）；commit 后 `publish_after_commit` 通知主执行人（SSE + 站内通知，通知正文只含结论摘要，**不含反馈正文**）。

### 5.3 反馈可见性（16 节，原则 6）

`GET /work-items/{id}/reviews` 服务端鉴权：**仅负责人与该工作项主执行人**，协作者与无关成员一律 403（有测试断言）。反馈正文只进 reviews 表与审计 after，不进通知、不进全员透明范围。

## 6. 前端（T4.6，13.1/13.2 节）

沿用 shadcn/ui 硬性约定：新增组件仅 `progress`，由 `npx shadcn@latest add progress` 生成入库。

### 6.1 页面结构

- **`features/deliverables/`**：
  - `DeliverableSection.tsx`（挂进 `WorkItemDetailPage`，协作区之上/之下与其他 section 同构）：版本历史（版本号、类型 Badge、内容摘要、提交人、时间）+ "提交交付" Dialog（三类型切换）+ 审核反馈区（`GET reviews` 403 时静默不渲染——协作者/无关成员天然不可见）。提交入口仅当前主执行人且非终态可见；列表 403（无关成员）整个区不渲染。
  - `FileUploadField.tsx`：选择即上传，**XHR onprogress 进度条**（fetch 不支持上传进度），前置校验 20MB/扩展名白名单（`constants.ts` 与后端一致），失败后"重试"按钮。file 类交付物流程 = 先 `POST /files`（带 `work_item_id`）拿 file_id，再提交 deliverable。
  - `DeliverableBody.tsx`：三类内容渲染（git 链接/文本/文件条目含文件名+大小+sha256 截断+下载按钮），详情页与审批 Dialog 共用。
  - `constants.ts` 的 `downloadStoredFile`：blob → `URL.createObjectURL` → `a[download]`；403 时 toast"无权限下载该文件"。
- **`features/approvals/DeliveryReviewSection.tsx`**（审批中心新增"交付审核"页签，仅 leader）：`GET /work-items?status=IN_REVIEW` 卡片（标题/主执行人/当前版本摘要）；审核 Dialog 内版本下拉切换历史版本、reviews 历史、三选一结论（request_changes 强制 feedback），`POST reviews` 带幂等键。
- `WorkItemDetailPage`：submit 命令 onError 加 `DELIVERABLE_REQUIRED` 分支，提示"请先提交交付物，再提交审核"。
- `features/deliverables/DeliverablesPage.tsx` 保留路由占位，文案指向详情页/审批中心（规格未要求独立页面）。

### 6.2 api.ts 新增

- `api.upload`：XHR + FormData（不设 Content-Type，浏览器带 boundary），`onProgress` 回调，复用 token/401 刷新重试一次逻辑，带幂等键。
- `api.downloadFile`：blob 响应，从 Content-Disposition 解 RFC 5987 文件名，复用 401 刷新逻辑，失败抛 `ApiError`（供 403 提示）。

### 6.3 SSE 事件

`events.ts` 新增监听 `review.approved/changes_requested/rejected`（经 `notify()` outbox 实际发布，curl 长连接实测收到帧），映射失效 `work-items`+`reviews`+`approvals`。`deliverable.*`/`file.*` 无 SSE 发布，已注释说明，不监听。

## 7. 测试与配置

- 全部测试 `docker compose exec backend pytest` → **205 passed**（阶段 3 的 188 个无回归）。新增覆盖：storage provider 单测 4（写/读/删/存在性、注入）；files API 12（落库+哈希一致、超限/非法类型且无残留、DB 失败补偿删除、无关成员 403、相关成员/负责人 200、路径不可绕过、下载审计可查）；deliverables 9（版本 1/2/3 历史可查、非主执行人 403、无交付物 submit 422、sha256 追溯、终态拒新、并发 409、幂等重放）；reviews 8（三结论状态迁移+同事务审计、普通成员 403、协作者/无关成员读反馈 403、COMPLETED 拒新交付物、幂等重放）。
- 前端无测试框架，`npm run build`（tsc strict + noUnusedLocals）是唯一静态检查，已通过；另用 curl 冒烟脚本对运行中 compose 栈做过全流程验证（三类提交→submit→request_changes→再提交→approve→COMPLETED）。
- 新增配置：`STORAGE_BACKEND`、`STORAGE_ROOT`、`UPLOAD_MAX_BYTES`、`UPLOAD_ALLOWED_EXTENSIONS`、`UPLOAD_ALLOWED_MIME_TYPES`（compose/.env.example 已接）。
- 迁移序列追加：`0007_stored_files` → `0008_deliverables_reviews`（含 `collaboration_requests` 两个可空引用列）。

## 8. 已知取舍与阶段 5 衔接

1. `reject` 结论保持 `IN_REVIEW` 不变更不迁移状态（7.5 节"保持工作项继续执行"的最直读法），负责人可要求成员改交新版本后再审。
2. `deliverable.submitted`/`file.*` 只写审计不发 SSE（看板刷新非必需，避免打扰）；若阶段 5 需要实时感知交付事件，在 service 加 `notify(outbox=...)` 即可。
3. 上传大小校验在流式暂存期间进行，超限即断流删暂存；客户端前置校验只是体验优化，后端白名单为准。
4. `python-multipart` 已进 backend 镜像；worker/scheduler 不 import files 路由，旧镜像无影响，`docker compose build` 全量重建即对齐。
5. 源码打进镜像而非挂载：**改了后端代码必须重建镜像再跑测试**（首次跑出新代码不生效即此原因）。
6. 阶段 5（Agent 辅助）：`transfer_requests.agent_suggestion_id` 预留列、`agents/` 占位包已就位；交付物的 git 链接与文本内容是 Agent 代码审查/摘要的天然输入；`stored_files` 的 `storage_backend` 列为未来对象存储迁移预留。
7. 构建提示：清华 PyPI 镜像偶发故障会导致 pip install 失败，重试或临时 `--build-arg PIP_INDEX_URL=https://pypi.org/simple`（同阶段 3 指南 8.5）。
