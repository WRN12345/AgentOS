import { useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft,
  FolderKanban,
  LayoutDashboard,
  LogOut,
  Plus,
  RefreshCw,
  ScrollText,
  ShieldCheck,
  TriangleAlert,
  UserCog,
  Users,
} from "lucide-react";
import { toast } from "sonner";
import { SimpleMenu, simpleMenuItemClass } from "@/components/SimpleMenu";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Progress } from "@/components/ui/progress";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { useAuthStore } from "../../app/store";
import { api, errorMessage, newIdempotencyKey } from "../../services/api";
import { logout } from "../auth/session";
import { queryKeys } from "../../lib/queryKeys";
import { roleLabel } from "../../lib/roles";
import { ACTION_LABELS, TARGET_TYPE_LABELS } from "../../lib/auditLabels";
import {
  formatDateTime,
  PRIORITY_META,
  STATUS_META,
} from "../work-items/constants";
import type {
  AdminAttentionItem,
  AdminAttentionPage,
  AdminAuditEvent,
  AdminOverview,
  AdminProject,
  UserMe,
} from "../../types";
import { ChangeLeaderDialog } from "./change-leader-dialog";
import { CreateAccountDialog } from "./create-account-dialog";
import { CreateProjectDialog } from "./create-project-dialog";
import ProjectRequirementsPanel from "./ProjectRequirementsPanel";

export default function AdminConsolePage() {
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const queryClient = useQueryClient();
  const me = useAuthStore((s) => s.user);
  const [createOpen, setCreateOpen] = useState(false);
  const [createAccountOpen, setCreateAccountOpen] = useState(false);
  const [leaderChangeTarget, setLeaderChangeTarget] =
    useState<AdminProject | null>(null);
  const [detail, setDetail] = useState<{
    context: string;
    id: AdminAttentionItem["id"];
  } | null>(null);
  const navigation = [
    { value: "overview", label: "管理总览", icon: LayoutDashboard },
    { value: "projects", label: "项目管理", icon: FolderKanban },
    { value: "people", label: "人员工作", icon: Users },
    { value: "accounts", label: "账号管理", icon: UserCog },
    { value: "audit", label: "系统审计", icon: ScrollText },
  ];
  const tab =
    navigation.find((item) => item.value === params.get("tab"))?.value ??
    "overview";
  const projectId =
    tab === "overview" || tab === "projects" ? params.get("project") : null;
  const scope = params.get("scope") || "all";
  const search = params.get("q") || "";
  const readPage = (key: string) => {
    const value = Number(params.get(key) || 1);
    return Number.isSafeInteger(value) &&
      value > 0 &&
      value <= Math.floor(Number.MAX_SAFE_INTEGER / 50)
      ? value
      : 1;
  };
  const page = readPage("page");
  const attentionPage = readPage("attentionPage");
  const auditPage = readPage("auditPage");
  const updateParams = (
    values: Record<string, string | null>,
    replace = false,
  ) => {
    setParams(
      (previous) => {
        const next = new URLSearchParams(previous);
        for (const [key, value] of Object.entries(values)) {
          if (value === null) next.delete(key);
          else next.set(key, value);
        }
        return next;
      },
      { replace },
    );
  };
  const openTab = (value: string) => {
    setDetail(null);
    setParams({ tab: value });
  };
  const openProject = (id: string | null) => {
    setDetail(null);
    updateParams({
      project: id,
      page: null,
      attentionPage: null,
      auditPage: null,
      q: null,
      scope: null,
    });
  };

  const overview = useQuery({
    queryKey: queryKeys.adminOverview(),
    queryFn: () => api.get<AdminOverview>("/admin/overview"),
  });
  const projects = overview.data?.projects ?? [];
  const selected = projects.find((project) => project.id === projectId);
  const attentionEnabled = tab === "overview" || Boolean(projectId);
  const auditEnabled = tab === "audit" || Boolean(projectId);
  const usersEnabled = tab === "accounts" || auditEnabled;
  const users = useQuery({
    queryKey: queryKeys.adminUsers(),
    queryFn: () => api.get<UserMe[]>("/users"),
    enabled: usersEnabled,
  });
  const attentionParams = new URLSearchParams({
    limit: "50",
    offset: String((attentionPage - 1) * 50),
  });
  if (projectId) attentionParams.set("project_id", projectId);
  const attention = useQuery({
    queryKey: queryKeys.adminAttention(projectId ?? "all", attentionPage, 50),
    queryFn: () =>
      api.get<AdminAttentionPage>(`/admin/attention?${attentionParams}`),
    enabled: attentionEnabled,
    placeholderData: undefined,
  });
  const auditScope = projectId ? `project:${projectId}` : scope;
  const auditSearch = projectId ? "" : search.trim();
  const auditQuery =
    Object.entries(ACTION_LABELS).find(
      ([, label]) => label === auditSearch,
    )?.[0] ?? auditSearch;
  const auditParams = new URLSearchParams({
    limit: "50",
    offset: String((auditPage - 1) * 50),
  });
  if (projectId) auditParams.set("project_id", projectId);
  else if (scope === "platform") auditParams.set("platform_only", "true");
  else if (scope !== "all") auditParams.set("project_id", scope);
  if (auditQuery) auditParams.set("q", auditQuery);
  const events = useQuery({
    queryKey: queryKeys.adminAuditEvents(auditScope, auditPage, 50, auditQuery),
    queryFn: () => api.get<AdminAuditEvent[]>(`/audit-events?${auditParams}`),
    enabled: auditEnabled,
    placeholderData: undefined,
  });
  const activeQueries = [
    { label: "管理统计", query: overview },
    ...(usersEnabled ? [{ label: "账号", query: users }] : []),
    ...(attentionEnabled ? [{ label: "关注任务", query: attention }] : []),
    ...(auditEnabled ? [{ label: "审计记录", query: events }] : []),
  ];
  const toggleActive = useMutation({
    mutationFn: (target: UserMe) =>
      api.patch<UserMe>(
        `/users/${target.id}`,
        { is_active: !target.is_active },
        newIdempotencyKey(),
      ),
    onSuccess: (updated) => {
      toast.success(
        `已${updated.is_active ? "启用" : "禁用"}账号 ${updated.username}`,
      );
      queryClient.invalidateQueries({ queryKey: queryKeys.adminUsers() });
      queryClient.invalidateQueries({ queryKey: queryKeys.adminOverview() });
      queryClient.invalidateQueries({ queryKey: queryKeys.adminAuditEvents() });
    },
    onError: (error) => toast.error(errorMessage(error, "账号状态变更失败")),
  });
  const handleLogout = async () => {
    try {
      await logout();
      navigate("/login", { replace: true });
    } catch (error) {
      toast.error(errorMessage(error, "登出失败，请重试"));
    }
  };
  const projectName = (id: string) =>
    projects.find((project) => project.id === id)?.name ?? id;
  const names = new Map(
    (users.data ?? []).map((user) => [user.id, user.username]),
  );
  const totals = projects.reduce(
    (sum, project) => ({
      total: sum.total + project.total,
      completed: sum.completed + project.completed,
      active: sum.active + project.active,
      overdue: sum.overdue + project.overdue,
      blocked: sum.blocked + project.blocked,
    }),
    { total: 0, completed: 0, active: 0, overdue: 0, blocked: 0 },
  );
  const needle = search.trim().toLocaleLowerCase();
  const filteredProjects = projects.filter((project) =>
    `${project.name} ${project.leader?.display_name ?? ""} ${project.leader?.username ?? ""}`
      .toLocaleLowerCase()
      .includes(needle),
  );
  const filteredMembers = (overview.data?.members ?? []).filter(
    (member) =>
      (!projectId || member.project_id === projectId) &&
      (projectId || scope === "all" || member.project_id === scope) &&
      `${member.display_name} ${member.username}`
        .toLocaleLowerCase()
        .includes(needle),
  );
  const filteredUsers = (users.data ?? []).filter((user) =>
    user.username.toLocaleLowerCase().includes(needle),
  );
  const localTotal =
    projectId || tab === "people"
      ? filteredMembers.length
      : tab === "accounts"
        ? filteredUsers.length
        : filteredProjects.length;
  const localPageCount = Math.max(1, Math.ceil(localTotal / 20));
  const localPage = Math.min(page, localPageCount);
  const start = (localPage - 1) * 20;
  const pagination = (
    <div className="mt-4 flex flex-wrap items-center justify-between gap-3 text-sm text-muted-foreground">
      <span>
        共 {localTotal} 条 · 每页 20 条 · 第 {localPage} / {localPageCount} 页
      </span>
      <div className="flex gap-2">
        <Button
          variant="outline"
          size="sm"
          disabled={localPage === 1}
          onClick={() => updateParams({ page: String(localPage - 1) })}
        >
          上一页
        </Button>
        <Button
          variant="outline"
          size="sm"
          disabled={localPage === localPageCount}
          onClick={() => updateParams({ page: String(localPage + 1) })}
        >
          下一页
        </Button>
      </div>
    </div>
  );
  const projectTable = (
    <>
      <Table>
        <TableHeader>
          <TableRow>
            {[
              "项目 / 负责人",
              "任务完成比例",
              "活跃任务",
              "逾期",
              "阻塞",
              "操作",
            ].map((label) => (
              <TableHead key={label}>{label}</TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {filteredProjects.slice(start, start + 20).map((project) => (
            <TableRow key={project.id}>
              <TableCell className="max-w-xs whitespace-normal">
                <button
                  className="text-left font-medium hover:underline"
                  onClick={() => openProject(project.id)}
                >
                  {project.name}
                </button>
                <p className="mt-1 text-xs text-muted-foreground">
                  {project.leader?.display_name ?? "未指定负责人"}
                </p>
                <p className="mt-1 text-xs text-muted-foreground">
                  {project.description}
                </p>
              </TableCell>
              <TableCell className="min-w-40">
                <div className="mb-2 flex justify-between gap-3 text-xs">
                  <span>
                    {project.completed} / {project.total}
                  </span>
                  <span>
                    {project.total
                      ? `${Math.round((project.completed / project.total) * 100)}%`
                      : "暂无任务"}
                  </span>
                </div>
                <Progress
                  aria-label={`${project.name}任务完成比例`}
                  value={
                    project.total
                      ? (project.completed / project.total) * 100
                      : 0
                  }
                  className="h-1.5"
                />
              </TableCell>
              <TableCell>{project.active}</TableCell>
              <TableCell className={project.overdue ? "text-destructive" : ""}>
                {project.overdue}
              </TableCell>
              <TableCell>{project.blocked}</TableCell>
              <TableCell>
                <div className="flex flex-wrap gap-1">
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => openProject(project.id)}
                  >
                    查看进度
                  </Button>
                  {tab === "projects" && (
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => setLeaderChangeTarget(project)}
                    >
                      变更负责人
                    </Button>
                  )}
                </div>
              </TableCell>
            </TableRow>
          ))}
          {filteredProjects.length === 0 && (
            <TableRow>
              <TableCell
                colSpan={6}
                className="py-8 text-center text-muted-foreground"
              >
                {search ? "没有匹配的项目" : "暂无项目"}
              </TableCell>
            </TableRow>
          )}
        </TableBody>
      </Table>
      {pagination}
    </>
  );
  const peopleTable = (
    <>
      <Table>
        <TableHeader>
          <TableRow>
            {[
              "人员",
              "项目 / 角色",
              "活跃任务",
              "近30天完成",
              "累计完成",
              "逾期 / 阻塞",
              "按时率",
            ].map((label) => (
              <TableHead key={label}>{label}</TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {filteredMembers.slice(start, start + 20).map((member) => (
            <TableRow key={member.member_id}>
              <TableCell>
                <div className="font-medium">{member.display_name}</div>
                <div className="text-xs text-muted-foreground">
                  {member.username}
                </div>
                {!member.is_active && (
                  <Badge variant="outline">成员已停用</Badge>
                )}
                {!member.user_is_active && (
                  <Badge variant="destructive">账号已禁用</Badge>
                )}
              </TableCell>
              <TableCell>
                {projectName(member.project_id)}
                <div className="text-xs text-muted-foreground">
                  {roleLabel(member.role)}
                </div>
              </TableCell>
              <TableCell>{member.active}</TableCell>
              <TableCell>{member.completed_recent}</TableCell>
              <TableCell>{member.completed_total}</TableCell>
              <TableCell>
                {member.overdue} / {member.blocked}
              </TableCell>
              <TableCell>
                {!member.sample_sufficient
                  ? "样本不足"
                  : member.on_time_rate === null
                    ? "暂无数据"
                    : `${Math.round(member.on_time_rate * 100)}%`}
              </TableCell>
            </TableRow>
          ))}
          {filteredMembers.length === 0 && (
            <TableRow>
              <TableCell
                colSpan={7}
                className="py-8 text-center text-muted-foreground"
              >
                暂无符合条件的成员
              </TableCell>
            </TableRow>
          )}
        </TableBody>
      </Table>
      {pagination}
      <p className="mt-4 text-xs leading-relaxed text-muted-foreground">
        按主执行人统计，跨项目分别展示。近30天完成与按时率以任务 updated_at
        近似统计；样本不足时不展示百分比。任务数量不代表投入工时或绩效排名。
      </p>
    </>
  );
  const attentionContext = `${tab}:${projectId ?? "all"}:${attentionPage}`;
  const attentionCard = attentionEnabled && (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <TriangleAlert className="size-4 text-destructive" />
          需要关注
        </CardTitle>
        <CardDescription>
          已逾期、阻塞或7日内到期的活跃任务。逾期与阻塞可能重叠。
        </CardDescription>
      </CardHeader>
      <CardContent>
        {attention.data && (
          <>
            <div className="divide-y">
              {attention.data.items.map((item) => (
                <button
                  key={item.id}
                  className="flex w-full items-start justify-between gap-3 rounded-md px-2 py-4 text-left hover:bg-muted/60"
                  onClick={() =>
                    setDetail({ context: attentionContext, id: item.id })
                  }
                >
                  <div className="min-w-0">
                    <p className="break-words text-sm font-medium">
                      {item.title}
                    </p>
                    <p className="mt-1 text-xs text-muted-foreground">
                      {projectName(item.project_id)} · {item.assignee_name}
                    </p>
                    <p
                      className={`mt-2 text-xs ${item.is_overdue ? "text-destructive" : "text-muted-foreground"}`}
                    >
                      {item.is_overdue ? "已逾期 · " : ""}截止：
                      {formatDateTime(item.due_at)}
                    </p>
                  </div>
                  <Badge className={STATUS_META[item.status].className}>
                    {STATUS_META[item.status].label}
                  </Badge>
                </button>
              ))}
            </div>
            {attention.data.items.length === 0 && (
              <p className="py-6 text-sm text-muted-foreground">
                本页暂无关注任务
              </p>
            )}
            <div className="mt-4 space-y-3 text-xs text-muted-foreground">
              <p>
                共 {attention.data.total} 条 · 每页 50 条 · 第 {attentionPage}{" "}
                页
              </p>
              <div className="flex gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  disabled={attentionPage === 1 || attention.isFetching}
                  onClick={() =>
                    updateParams({ attentionPage: String(attentionPage - 1) })
                  }
                >
                  上一页
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={
                    attentionPage * 50 >= attention.data.total ||
                    attention.isFetching
                  }
                  onClick={() =>
                    updateParams({ attentionPage: String(attentionPage + 1) })
                  }
                >
                  下一页
                </Button>
              </div>
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
  const auditCard = auditEnabled && (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle>{projectId ? "项目动态" : "操作记录"}</CardTitle>
        {!projectId && (
          <div className="mt-3 flex flex-wrap gap-3">
            <Input
              aria-label="搜索审计记录"
              placeholder="搜索操作者、动作或对象"
              className="max-w-xs"
              value={search}
              maxLength={200}
              onChange={(event) =>
                updateParams({ q: event.target.value, auditPage: null }, true)
              }
            />
            <select
              aria-label="审计范围"
              className="w-full rounded-md border bg-background px-3 py-2 text-sm sm:max-w-xs"
              value={scope}
              onChange={(event) =>
                updateParams({ scope: event.target.value, auditPage: null })
              }
            >
              <option value="all">全部范围</option>
              <option value="platform">平台管理</option>
              {projects.map((project) => (
                <option key={project.id} value={project.id}>
                  {project.name}
                </option>
              ))}
            </select>
          </div>
        )}
      </CardHeader>
      <CardContent>
        {events.data && (
          <>
            <Table>
              <TableHeader>
                <TableRow>
                  {["时间", "操作者", "动作", "操作对象", "所属范围"].map(
                    (label) => (
                      <TableHead key={label}>{label}</TableHead>
                    ),
                  )}
                </TableRow>
              </TableHeader>
              <TableBody>
                {events.data.map((event) => (
                  <TableRow key={event.id}>
                    <TableCell className="text-muted-foreground">
                      {formatDateTime(event.created_at)}
                    </TableCell>
                    <TableCell>
                      {event.actor_id
                        ? (names.get(event.actor_id) ?? event.actor_id)
                        : "系统"}
                    </TableCell>
                    <TableCell>
                      <Badge variant="secondary">
                        {ACTION_LABELS[event.action] ?? event.action}
                      </Badge>
                    </TableCell>
                    <TableCell className="max-w-xs whitespace-normal break-all">
                      {event.target_type
                        ? (TARGET_TYPE_LABELS[event.target_type] ??
                          event.target_type)
                        : "-"}
                      {event.target_id && (
                        <div className="text-xs text-muted-foreground">
                          {event.target_id}
                        </div>
                      )}
                    </TableCell>
                    <TableCell>
                      {event.project_id === null
                        ? "平台管理"
                        : projectName(event.project_id)}
                    </TableCell>
                  </TableRow>
                ))}
                {events.data.length === 0 && (
                  <TableRow>
                    <TableCell
                      colSpan={5}
                      className="py-8 text-center text-muted-foreground"
                    >
                      本页暂无{projectId ? "项目动态" : "审计记录"}
                    </TableCell>
                  </TableRow>
                )}
              </TableBody>
            </Table>
            <div className="mt-4 flex flex-wrap items-center justify-between gap-3 text-sm text-muted-foreground">
              <span>
                第 {auditPage} 页 · 每页最多 50 条 · 本页 {events.data.length}{" "}
                条
              </span>
              <div className="flex gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  disabled={auditPage === 1 || events.isFetching}
                  onClick={() =>
                    updateParams({ auditPage: String(auditPage - 1) })
                  }
                >
                  上一页
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={events.data.length < 50 || events.isFetching}
                  onClick={() =>
                    updateParams({ auditPage: String(auditPage + 1) })
                  }
                >
                  下一页
                </Button>
              </div>
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
  const stats = selected ?? totals;
  const detailItem =
    detail?.context === attentionContext && attentionEnabled
      ? (attention.data?.items.find((item) => item.id === detail.id) ?? null)
      : null;

  return (
    <div className="flex h-dvh overflow-hidden">
      <aside className="flex w-16 shrink-0 flex-col border-r bg-sidebar md:w-56">
        <div className="flex h-14 shrink-0 items-center justify-center px-4 md:justify-start">
          <span className="hidden text-lg font-semibold md:inline">
            AgentOS
          </span>
          <ShieldCheck aria-label="AgentOS" className="size-5 md:hidden" />
        </div>
        <Separator />
        <nav
          aria-label="管理员导航"
          className="flex-1 space-y-1 overflow-y-auto p-2"
        >
          {navigation.map(({ value, label, icon: Icon }) => (
            <button
              key={value}
              type="button"
              title={label}
              aria-current={tab === value ? "page" : undefined}
              onClick={() => openTab(value)}
              className={`flex w-full items-center justify-center gap-2 rounded-md px-3 py-2 text-sm font-medium focus-visible:outline-2 focus-visible:outline-ring md:justify-start ${tab === value ? "bg-sidebar-accent text-sidebar-accent-foreground" : "text-sidebar-foreground/70 hover:bg-sidebar-accent/60"}`}
            >
              <Icon aria-hidden="true" className="size-4 shrink-0" />
              <span className="sr-only md:not-sr-only">{label}</span>
            </button>
          ))}
        </nav>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-14 shrink-0 items-center justify-between gap-2 border-b bg-background px-3 sm:px-6">
          <span className="truncate text-sm font-medium">管理控制台</span>
          <SimpleMenu
            trigger={(toggle, open) => (
              <button
                type="button"
                aria-label="账号菜单"
                aria-haspopup="menu"
                aria-expanded={open}
                onClick={toggle}
                className="flex shrink-0 items-center gap-2.5 rounded-md p-1 text-left hover:bg-muted"
              >
                <span className="flex size-8 shrink-0 items-center justify-center rounded-full bg-muted text-muted-foreground">
                  <ShieldCheck aria-hidden="true" className="size-4" />
                </span>
                <span className="min-w-0 max-w-28 leading-tight sm:max-w-40">
                  <span
                    className="block truncate text-sm font-medium"
                    title={me?.username}
                  >
                    {me?.username ?? "管理员"}
                  </span>
                  <span className="mt-0.5 block text-xs text-muted-foreground">
                    全局管理员
                  </span>
                </span>
              </button>
            )}
            contentClassName="w-40"
          >
            {(close) => (
              <button
                role="menuitem"
                className={simpleMenuItemClass}
                onClick={() => {
                  close();
                  void handleLogout();
                }}
              >
                <LogOut className="size-4" />
                登出
              </button>
            )}
          </SimpleMenu>
        </header>
        <main className="min-h-0 flex-1 overflow-y-auto bg-muted/40 p-4 sm:p-6">
          <div className="mx-auto max-w-7xl space-y-6">
            <section className="flex flex-wrap items-end justify-between gap-3">
              <div>
                {projectId && (
                  <Button
                    variant="ghost"
                    size="sm"
                    className="mb-3"
                    onClick={() => openProject(null)}
                  >
                    <ArrowLeft className="size-4" />
                    返回项目列表
                  </Button>
                )}
                <h1 className="text-2xl font-semibold tracking-tight">
                  {projectId
                    ? (selected?.name ?? "项目监督")
                    : navigation.find((item) => item.value === tab)?.label}
                </h1>
                <p className="mt-2 text-sm text-muted-foreground">
                  {projectId
                    ? `${tab === "projects" ? "任务监督 · 只读" : "项目监督 · 只读"}${selected ? ` · 负责人：${selected.leader?.display_name ?? "未指定"}` : ""}`
                    : {
                        overview: "掌握项目进展，关注交付风险与人员工作情况。",
                        projects: "创建项目、指定负责人，跟踪项目任务进展。",
                        people: "按主执行人统计任务与近期产出，保留项目归属。",
                        accounts: "创建平台账号，管理账号的启用与禁用状态。",
                        audit: "查看平台管理操作与项目业务操作记录。",
                      }[tab]}
                </p>
                {selected?.description && (
                  <p className="mt-2 text-sm text-muted-foreground">
                    {selected.description}
                  </p>
                )}
              </div>
              <div className="flex flex-wrap gap-2">
                <Button
                  variant="outline"
                  disabled={activeQueries.some(({ query }) => query.isFetching)}
                  onClick={() => {
                    for (const { query } of activeQueries) void query.refetch();
                  }}
                >
                  <RefreshCw className="size-4" />
                  刷新
                </Button>
                {tab === "projects" && !projectId && (
                  <Button onClick={() => setCreateOpen(true)}>
                    <Plus className="size-4" />
                    新建项目
                  </Button>
                )}
                {tab === "accounts" && (
                  <Button onClick={() => setCreateAccountOpen(true)}>
                    <Plus className="size-4" />
                    新建账号
                  </Button>
                )}
              </div>
            </section>

            <div className="flex flex-wrap gap-x-5 gap-y-2 text-xs text-muted-foreground">
              {overview.data && (
                <span>统计截至 {formatDateTime(overview.data.as_of)}</span>
              )}
              {activeQueries
                .filter(
                  ({ label, query }) =>
                    label !== "管理统计" && query.dataUpdatedAt > 0,
                )
                .map(({ label, query }) => (
                  <span key={label}>
                    {label}获取于{" "}
                    {formatDateTime(
                      new Date(query.dataUpdatedAt).toISOString(),
                    )}
                  </span>
                ))}
            </div>
            {activeQueries.map(({ label, query }) => (
              <div key={label} className="empty:hidden">
                {query.isPending && (
                  <div role="status" className="space-y-2">
                    <p className="text-sm text-muted-foreground">
                      正在加载{label}...
                    </p>
                    <Skeleton className="h-12 w-full" />
                  </div>
                )}
                {query.isError && (
                  <div
                    role="alert"
                    className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-destructive/40 bg-background p-4 text-sm"
                  >
                    <span className="text-destructive">
                      {label}
                      {query.data
                        ? "刷新失败，当前显示上次成功获取的数据。"
                        : "加载失败。"}{" "}
                      {errorMessage(query.error, "请稍后重试")}
                    </span>
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={query.isFetching}
                      onClick={() => void query.refetch()}
                    >
                      重试{label}
                    </Button>
                  </div>
                )}
                {query.isFetching && !query.isPending && (
                  <p role="status" className="text-xs text-muted-foreground">
                    正在刷新{label}...
                  </p>
                )}
              </div>
            ))}

            {projectId && overview.data && !selected && (
              <p className="rounded-lg border bg-background p-6 text-sm">
                未找到该项目，请返回项目列表。
              </p>
            )}
            {overview.data &&
              (selected || (!projectId && tab === "overview")) && (
                <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
                  {[
                    {
                      label: selected ? "任务完成比例" : "项目总数",
                      value: selected
                        ? stats.total
                          ? `${Math.round((stats.completed / stats.total) * 100)}%`
                          : "暂无任务"
                        : projects.length,
                      caption: stats.total
                        ? `已完成 ${stats.completed} / ${stats.total} 个有效任务`
                        : "暂无任务",
                    },
                    {
                      label: "活跃任务",
                      value: stats.active,
                      caption: "尚未完成的任务",
                    },
                    {
                      label: "已逾期任务",
                      value: stats.overdue,
                      caption: "超过截止时间且未完成",
                    },
                    {
                      label: "阻塞任务",
                      value: stats.blocked,
                      caption: "当前处于阻塞状态",
                    },
                  ].map(({ label, value, caption }) => (
                    <Card key={label}>
                      <CardHeader>
                        <CardDescription>{label}</CardDescription>
                        <CardTitle className="text-3xl tabular-nums">
                          {value}
                        </CardTitle>
                      </CardHeader>
                      <CardContent className="text-xs text-muted-foreground">
                        {caption}
                      </CardContent>
                    </Card>
                  ))}
                </div>
              )}

            {selected ? (
              <>
                {tab === "projects" && <ProjectRequirementsPanel key={selected.id} projectId={selected.id} />}
                <div className="grid items-start gap-6 xl:grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)]">
                  <Card className="min-w-0">
                    <CardHeader>
                      <CardTitle>成员工作量</CardTitle>
                      <CardDescription>
                        当前项目的主执行任务与完成情况
                      </CardDescription>
                    </CardHeader>
                    <CardContent>{peopleTable}</CardContent>
                  </Card>
                  {attentionCard}
                </div>
                {auditCard}
              </>
            ) : (
              !projectId && (
                <>
                  {(tab === "overview" || tab === "projects") && (
                    <div
                      className={
                        tab === "overview"
                          ? "grid items-start gap-6 xl:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]"
                          : ""
                      }
                    >
                      <div className="min-w-0 space-y-6">
                        <Card>
                          <CardHeader>
                            <CardTitle>项目进展</CardTitle>
                            <CardDescription>
                              任务完成比例 = 已完成 /
                              已发布且非取消任务；分母排除草稿与取消。
                            </CardDescription>
                            {tab === "projects" && (
                              <Input
                                aria-label="搜索项目或负责人"
                                placeholder="搜索项目或负责人"
                                value={search}
                                onChange={(event) =>
                                  updateParams(
                                    { q: event.target.value, page: null },
                                    true,
                                  )
                                }
                                className="mt-3 max-w-sm"
                              />
                            )}
                          </CardHeader>
                          <CardContent>
                            {overview.data && projectTable}
                          </CardContent>
                        </Card>
                        {tab === "overview" && overview.data && (
                          <Card>
                            <CardHeader className="flex-row items-center justify-between gap-3">
                              <div>
                                <CardTitle>人员工作概况</CardTitle>
                                <CardDescription className="mt-2">
                                  共{" "}
                                  {
                                    new Set(
                                      overview.data.members.map(
                                        (member) => member.user_id,
                                      ),
                                    ).size
                                  }{" "}
                                  人 · {overview.data.members.length}{" "}
                                  条项目成员记录（含停用）
                                </CardDescription>
                              </div>
                              <Button
                                variant="ghost"
                                size="sm"
                                onClick={() => openTab("people")}
                              >
                                查看全部
                              </Button>
                            </CardHeader>
                            <CardContent>
                              <div className="grid gap-4 sm:grid-cols-3">
                                {overview.data.members
                                  .slice(0, 3)
                                  .map((member) => (
                                    <div
                                      key={member.member_id}
                                      className="rounded-lg border p-4"
                                    >
                                      <div className="text-sm font-medium">
                                        {member.display_name}
                                      </div>
                                      <p className="mt-1 text-xs text-muted-foreground">
                                        {projectName(member.project_id)} ·{" "}
                                        {roleLabel(member.role)}
                                      </p>
                                      {!member.is_active && (
                                        <Badge variant="outline">
                                          成员已停用
                                        </Badge>
                                      )}
                                      {!member.user_is_active && (
                                        <Badge variant="destructive">
                                          账号已禁用
                                        </Badge>
                                      )}
                                      <p className="mt-4 text-2xl font-semibold">
                                        {member.active}
                                        <span className="ml-2 text-xs font-normal text-muted-foreground">
                                          活跃任务
                                        </span>
                                      </p>
                                      <p className="mt-2 text-xs text-muted-foreground">
                                        近30天完成 {member.completed_recent} ·
                                        逾期 {member.overdue}
                                      </p>
                                    </div>
                                  ))}
                              </div>
                              {overview.data.members.length === 0 && (
                                <p className="text-sm text-muted-foreground">
                                  暂无成员
                                </p>
                              )}
                              <p className="mt-4 text-xs leading-relaxed text-muted-foreground">
                                近30天完成以任务 updated_at
                                近似统计。任务数量不代表投入工时或绩效排名；跨项目成员分别展示。
                              </p>
                            </CardContent>
                          </Card>
                        )}
                      </div>
                      {tab === "overview" && attentionCard}
                    </div>
                  )}
                  {tab === "people" && (
                    <Card>
                      <CardHeader>
                        <CardTitle>人员任务与产出</CardTitle>
                        {overview.data && (
                          <CardDescription>
                            当前筛选共{" "}
                            {
                              new Set(
                                filteredMembers.map((member) => member.user_id),
                              ).size
                            }{" "}
                            人 · {filteredMembers.length} 条项目成员记录
                          </CardDescription>
                        )}
                        <div className="mt-3 flex flex-wrap gap-3">
                          <Input
                            className="max-w-xs"
                            aria-label="搜索人员"
                            placeholder="搜索姓名或账号"
                            value={search}
                            onChange={(event) =>
                              updateParams(
                                { q: event.target.value, page: null },
                                true,
                              )
                            }
                          />
                          <select
                            aria-label="筛选项目"
                            className="rounded-md border bg-background px-3 py-2 text-sm"
                            value={scope}
                            onChange={(event) =>
                              updateParams({
                                scope: event.target.value,
                                page: null,
                              })
                            }
                          >
                            <option value="all">全部项目</option>
                            {projects.map((project) => (
                              <option key={project.id} value={project.id}>
                                {project.name}
                              </option>
                            ))}
                          </select>
                        </div>
                      </CardHeader>
                      <CardContent>{overview.data && peopleTable}</CardContent>
                    </Card>
                  )}
                  {tab === "accounts" && (
                    <Card>
                      <CardHeader>
                        <CardTitle>平台账号</CardTitle>
                        {users.data && (
                          <CardDescription>
                            共 {users.data.length} 个账号 ·{" "}
                            {users.data.filter((user) => user.is_active).length}{" "}
                            个已启用
                          </CardDescription>
                        )}
                        <Input
                          className="mt-3 max-w-sm"
                          aria-label="搜索账号"
                          placeholder="搜索账号"
                          value={search}
                          onChange={(event) =>
                            updateParams(
                              { q: event.target.value, page: null },
                              true,
                            )
                          }
                        />
                      </CardHeader>
                      <CardContent>
                        {users.data && (
                          <>
                            <Table>
                              <TableHeader>
                                <TableRow>
                                  {["账号", "身份", "状态", "操作"].map(
                                    (label) => (
                                      <TableHead key={label}>{label}</TableHead>
                                    ),
                                  )}
                                </TableRow>
                              </TableHeader>
                              <TableBody>
                                {filteredUsers
                                  .slice(start, start + 20)
                                  .map((user) => (
                                    <TableRow key={user.id}>
                                      <TableCell>
                                        <div className="font-medium">
                                          {user.username}
                                        </div>
                                        <div className="text-xs text-muted-foreground">
                                          注册于{" "}
                                          {formatDateTime(user.created_at)}
                                        </div>
                                      </TableCell>
                                      <TableCell>
                                        {user.is_admin
                                          ? "全局管理员"
                                          : "普通账号"}
                                      </TableCell>
                                      <TableCell>
                                        <Badge
                                          variant={
                                            user.is_active
                                              ? "secondary"
                                              : "destructive"
                                          }
                                        >
                                          {user.is_active ? "已启用" : "已禁用"}
                                        </Badge>
                                      </TableCell>
                                      <TableCell>
                                        {user.id === me?.id ? (
                                          <span className="text-xs text-muted-foreground">
                                            当前账号
                                          </span>
                                        ) : (
                                          <Button
                                            variant="ghost"
                                            size="sm"
                                            disabled={toggleActive.isPending}
                                            onClick={() =>
                                              toggleActive.mutate(user)
                                            }
                                          >
                                            {user.is_active ? "禁用" : "启用"}
                                          </Button>
                                        )}
                                      </TableCell>
                                    </TableRow>
                                  ))}
                                {filteredUsers.length === 0 && (
                                  <TableRow>
                                    <TableCell
                                      colSpan={4}
                                      className="py-8 text-center text-muted-foreground"
                                    >
                                      暂无符合条件的账号
                                    </TableCell>
                                  </TableRow>
                                )}
                              </TableBody>
                            </Table>
                            {pagination}
                          </>
                        )}
                      </CardContent>
                    </Card>
                  )}
                  {tab === "audit" && auditCard}
                </>
              )
            )}
          </div>
        </main>
      </div>
      <CreateProjectDialog open={createOpen} onOpenChange={setCreateOpen} />
      <CreateAccountDialog
        open={createAccountOpen}
        onOpenChange={setCreateAccountOpen}
      />
      <ChangeLeaderDialog
        project={leaderChangeTarget}
        onClose={() => setLeaderChangeTarget(null)}
      />
      <Dialog
        open={detailItem !== null}
        onOpenChange={(open) => {
          if (!open) setDetail(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{detailItem?.title ?? "任务摘要"}</DialogTitle>
            <DialogDescription>
              {detailItem ? projectName(detailItem.project_id) : ""} · 任务监督
            </DialogDescription>
          </DialogHeader>
          {detailItem && (
            <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-4 text-sm">
              <dt className="text-muted-foreground">主执行人</dt>
              <dd>{detailItem.assignee_name}</dd>
              <dt className="text-muted-foreground">状态</dt>
              <dd>{STATUS_META[detailItem.status].label}</dd>
              <dt className="text-muted-foreground">优先级</dt>
              <dd>{PRIORITY_META[detailItem.priority].label}</dd>
              <dt className="text-muted-foreground">截止时间</dt>
              <dd>{formatDateTime(detailItem.due_at)}</dd>
              <dt className="text-muted-foreground">更新时间</dt>
              <dd>{formatDateTime(detailItem.updated_at)}</dd>
              <dt className="text-muted-foreground">逾期状态</dt>
              <dd>{detailItem.is_overdue ? "已逾期" : "未逾期"}</dd>
            </dl>
          )}
          <p className="text-xs text-muted-foreground">
            管理员只读查看；任务分配与业务处理由项目负责人执行。
          </p>
        </DialogContent>
      </Dialog>
    </div>
  );
}
