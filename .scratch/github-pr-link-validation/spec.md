# Git 交付链接校验 spec

Status: ready-for-agent

> 日期：2026-08-25
> 来源：当前对话确认的最小变化方案
> 关联：阶段 4 交付与审核中的 Git 链接型交付物

## Problem Statement

AgentOS 的 Git 链接交付原本只检查内容非空，普通网页、仓库首页、Issue、不安全协议和伪造域名都可能被保存。负责人虽然已经可以点击链接，但只能在审核时发现链接不符合预期。

本次以最小变化收紧新提交入口，覆盖 GitHub、Gitee、GitLab 的评审链接与具体 Commit 链接。前端即时提示，后端权威校验并规范化保存。既有历史数据、版本、权限和审核流程不变。

## Supported URLs

| 平台 | 代码评审 | Commit |
| --- | --- | --- |
| GitHub | `https://github.com/{owner}/{repo}/pull/{N}` | `https://github.com/{owner}/{repo}/commit/{SHA}` |
| Gitee | `https://gitee.com/{owner}/{repo}/pulls/{N}` | `https://gitee.com/{owner}/{repo}/commit/{SHA}` |
| GitLab | `https://gitlab.com/{namespace...}/{repo}/-/merge_requests/{N}` | `https://gitlab.com/{namespace...}/{repo}/-/commit/{SHA}` |

`N` 必须是大于零的十进制序号。`SHA` 必须是 7–40 位十六进制字符，保存时统一转成小写。GitLab 支持嵌套 group，但至少包含 namespace 和 repo 两段。

允许输入首尾空白和一个尾部斜杠，保存时移除。拒绝查询参数、锚点、额外路径、用户信息、显式端口、非 HTTPS 协议、非精确支持主机，以及路径中的点段或编码后的路径分隔符。

## User Stories

1. 主执行人可以提交三平台支持的 PR、MR 或 Commit 链接，并立即获知格式错误。
2. 绕过前端提交非法链接时，后端返回统一 422 且不生成交付版本。
3. 负责人继续在任务详情和交付审核界面直接点击链接并在新窗口打开。
4. 历史 Git 链接不迁移、不清洗，新规则只约束新提交。
5. 文本、文件交付、版本递增、项目隔离、权限和审核状态机保持不变。

## Implementation Decisions

- 现有创建交付物接口是权威校验入口，不新增平台专用接口。
- 使用结构化 URL 解析结果判断协议、主机、端口、用户信息、查询参数和片段。
- 前端使用同一公开规则做即时反馈；后端重新校验，不能依赖前端结果。
- 请求仍使用现有 `type` 与 `content` 字段，响应和数据库结构不变。
- 评审序号保存为原十进制文本；Commit SHA 保存为小写；所有合法链接去除首尾空白和尾部斜杠。
- 校验失败不创建版本、不写提交审计、不推进工作项状态。
- 不调用 Git 平台接口，不检查远端对象是否存在或当前用户是否有访问权限。

## Testing Decisions

- 通过 `POST /work-items/{id}/deliverables` 测试，不只测试私有解析函数。
- 后端覆盖三平台评审和 Commit 成功路径、GitLab 嵌套 group、规范化，以及协议、主机、端口、用户信息、路径、查询、片段、序号和 SHA 非法组合。
- 失败请求后通过版本历史接口确认没有生成记录。
- 前端验证至少一个非 GitHub 合法链接可提交，并用规则矩阵覆盖三平台格式。
- 负责人既有流程继续验证链接可点击并在新窗口打开。
- 文本和文件交付回归测试保持不变。

## Out of Scope

- GitHub/Gitee/GitLab API、App、Token、OAuth、Webhook、评论同步和元数据快照。
- Bitbucket、自建 Git、GitHub Enterprise Server 或自定义域名。
- Branch、Tag、Release、仓库首页、Issue、Discussion 等非评审或 Commit 链接。
- 数据库迁移、历史数据清理、审核记录或工作项状态机修改。

## Further Notes

这里的“有效”只表示 URL 结构符合支持形状，不代表远端 PR、MR 或 Commit 存在、可访问或属于当前项目。私有仓库权限仍由负责人浏览器中的平台登录状态决定。
