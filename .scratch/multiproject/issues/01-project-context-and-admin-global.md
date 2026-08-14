# 01 — 项目上下文与 admin 全局化

**What to build:** 让"带项目上下文的请求"成为可能。所有业务请求通过 `X-Project-Id` 请求头携带项目上下文；缺失返回 400，携带了自己不是成员的项目返回 403。管理员升级为全局角色（`users.is_admin`），不属于任何项目、不参与业务协作，可通过专用接口管理平台。新增"我参与的项目"接口。测试基建支持 A/B 双项目与跨项目成员，既有测试套件保持全绿。

**Blocked by:** None — can start immediately

**Status:** resolved

- [x] 请求带 `X-Project-Id` 指向自己所在项目时，身份解析成功，后续服务能取得该项目下自己的成员角色
- [x] 请求缺失 `X-Project-Id` → 400
- [x] 请求带自己不是成员的项目 → 403
- [x] `users.is_admin` 标记的全局管理员：不属于任何项目；管理/审计类接口对 admin 放行（依赖成员身份的审计路径不再 403）
- [x] `GET /me/projects` 返回当前用户参与的项目列表
- [x] 非业务配置类接口不再依赖项目成员身份
- [x] conftest：A/B 两个项目 fixture、同一用户跨项目可持不同角色、`auth_headers` 自动携带 `X-Project-Id`，既有测试套件全绿
- [x] 引导脚本中 admin 相关逻辑改为全局 admin（`users.is_admin`），清理旧的 admin 成员分支
