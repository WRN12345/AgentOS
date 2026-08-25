# GitHub PR 交付链接校验 spec

Status: ready-for-agent

> 日期：2026-08-25
> 来源：当前对话确认的最小变化方案
> 关联：阶段 4 交付与审核中的 Git 链接型交付物

## Problem Statement

AgentOS 的 Git 链接交付原本只检查内容非空，普通网页、仓库首页、Issue、不安全协议和伪造域名都可能被保存。负责人虽然已经可以点击链接，但只能在审核时发现链接不符合预期。

本次以最小变化收紧新提交入口：仅接受标准 GitHub Pull Request URL，前端即时提示，后端权威校验并规范化保存。既有历史数据、版本、权限和审核流程不变。

## Solution

合法地址必须符合 `https://github.com/{owner}/{repo}/pull/{positive-integer}`。允许输入首尾空白和一个尾部斜杠，保存时移除。拒绝查询参数、锚点、额外路径、用户信息、显式端口、非 HTTPS 协议和非精确 `github.com` 主机。

创建交付物仍使用现有 `type` 与 `content` 字段，响应和数据库结构不变。不调用 GitHub 接口，不检查 PR 是否存在或是否有访问权限。

## User Stories

1. 主执行人可以提交标准 GitHub PR 链接，并立即获知格式错误。
2. 绕过前端提交非法链接时，后端返回统一 422 且不生成交付版本。
3. 负责人继续在任务详情和交付审核界面直接点击链接并在新窗口打开。
4. 历史 Git 链接不迁移、不清洗，新规则只约束新提交。
5. 文本、文件交付、版本递增、项目隔离、权限和审核状态机保持不变。

## Implementation Decisions

- 现有创建交付物接口是权威校验入口，不新增 GitHub 专用接口。
- 使用结构化 URL 解析结果判断协议、主机、端口、用户信息、查询参数和片段。
- 路径必须恰好包含 owner、repo、`pull` 和大于零的十进制 PR 序号。
- 允许首尾空白和一个尾部斜杠，保存时规范化为无尾部斜杠地址。
- 前端显示“请输入有效的 GitHub PR 链接”，后端使用相同可见规则并返回 422。
- 校验失败不创建版本、不写提交审计、不推进工作项状态。
- 数据库模型、请求响应、历史记录、幂等、版本、权限和审核逻辑不变。

## Testing Decisions

- 通过 `POST /work-items/{id}/deliverables` 测试，不直接测试私有解析函数。
- 后端覆盖标准地址、空白和尾部斜杠规范化，以及协议、主机、端口、用户信息、路径、查询、片段和序号非法组合。
- 失败请求后通过版本历史接口确认没有生成记录。
- 前端成员流程验证非法链接显示错误且不发请求。
- 负责人既有流程继续验证链接可点击并在新窗口打开。
- 文本和文件交付回归测试保持不变。

## Out of Scope

- GitHub API、GitHub App、Token、OAuth、Webhook、评论同步和元数据快照。
- Gitee、GitLab、Bitbucket、自建 Git 或 GitHub Enterprise Server。
- Commit、Branch、Tag、Release、仓库首页、Issue、Discussion 等非 PR 链接。
- 数据库迁移、历史数据清理、审核记录或工作项状态机修改。

## Further Notes

这里的“有效”只表示 URL 结构符合支持形状，不代表远端 PR 存在、可访问或属于当前项目。私有仓库权限仍由负责人浏览器中的 GitHub 登录状态决定。
