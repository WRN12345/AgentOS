## Agent skills

### Issue tracker

Work items (specs and tickets) live as local markdown files under `.scratch/<feature>/`. See `docs/agents/issue-tracker.md`.

### Triage labels

Five canonical roles, each label string equal to its name (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`), recorded as `Status:` lines in issue files. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.

## 仓库主约定

- git：一律调用 `agentos-git` skill（分支/提交/PR 规范见 skill）。
- 前端：一律用 shadcn/ui 组件（`src/components/ui/`）；`dialog` / `drawer` / `alert` 覆盖层组件绝对不要混用，同一类交互只选一种。
