import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, UserCheck, UserCog, UserX } from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/components/ui/tabs";
import { useAuthStore } from "../../app/store";
import { api, errorMessage, newIdempotencyKey } from "../../services/api";
import { logout } from "../auth/session";
import { queryKeys } from "../../lib/queryKeys";
import { ACTION_LABELS, TARGET_TYPE_LABELS } from "../../lib/auditLabels";
import { formatDateTime } from "../work-items/constants";
import type { AdminProject, AuditEvent, UserMe } from "../../types";
import { ChangeLeaderDialog } from "./change-leader-dialog";
import { CreateAccountDialog } from "./create-account-dialog";
import { CreateProjectDialog } from "./create-project-dialog";

/**
 * 管理控制台（ticket 10）：全局管理员的平台级工作台。
 * 仅全局管理员可访问（路由由 AdminOnly 守卫）。
 * 三个标签页：项目列表/新建、账号管理、审计查看，全部接口 admin-only。
 */
export default function AdminConsolePage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const me = useAuthStore((s) => s.user);

  const [createOpen, setCreateOpen] = useState(false);
  const [createAccountOpen, setCreateAccountOpen] = useState(false);
  const [leaderChangeTarget, setLeaderChangeTarget] =
    useState<AdminProject | null>(null);

  const { data: projects, isLoading: projectsLoading } = useQuery({
    queryKey: queryKeys.adminProjects(),
    queryFn: () => api.get<AdminProject[]>("/projects"),
  });

  const { data: users, isLoading: usersLoading } = useQuery({
    queryKey: queryKeys.adminUsers(),
    queryFn: () => api.get<UserMe[]>("/users"),
  });

  // 平台级审计：admin 可见全部事件（含 project_id=NULL 的全局事件）
  const { data: events } = useQuery({
    queryKey: queryKeys.adminAuditEvents(),
    queryFn: () => api.get<AuditEvent[]>("/audit-events?limit=50"),
  });

  // admin 启用/禁用账号（不能禁用自己，由后端校验；UI 同步隐藏本人行的禁用按钮）
  const toggleActive = useMutation({
    mutationFn: (target: UserMe) =>
      api.patch<UserMe>(
        `/users/${target.id}`,
        { is_active: !target.is_active },
        newIdempotencyKey(),
      ),
    onSuccess: (updated) => {
      toast.success(
        updated.is_active
          ? `已启用账号 ${updated.username}`
          : `已禁用账号 ${updated.username}`,
      );
      queryClient.invalidateQueries({ queryKey: queryKeys.adminUsers() });
      queryClient.invalidateQueries({ queryKey: queryKeys.adminAuditEvents() });
    },
    onError: (error) => toast.error(errorMessage(error, "账号状态变更失败")),
  });

  const nameByUserId = new Map(
    (users ?? []).map((u) => [u.id, u.username]),
  );
  const actorName = (e: AuditEvent) =>
    e.actor_id ? (nameByUserId.get(e.actor_id) ?? "未知账号") : "系统";

  const handleLogout = async () => {
    await logout();
    toast.success("已登出");
    navigate("/login", { replace: true });
  };

  return (
    <div className="min-h-screen bg-muted/40 p-6">
      <div className="mx-auto max-w-5xl space-y-6">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-semibold">管理控制台</h1>
            <p className="text-sm text-muted-foreground">
              欢迎，{me?.username ?? "管理员"}。平台级项目与账号管理。
            </p>
          </div>
          <Button variant="outline" onClick={() => void handleLogout()}>
            登出
          </Button>
        </div>

        <Tabs defaultValue="projects">
          <TabsList>
            <TabsTrigger value="projects">项目列表</TabsTrigger>
            <TabsTrigger value="users">账号管理</TabsTrigger>
            <TabsTrigger value="audit">审计</TabsTrigger>
          </TabsList>

          <TabsContent value="projects" className="mt-4">
            <Card>
              <CardHeader className="flex-row items-center justify-between space-y-0">
                <div>
                  <CardTitle>项目列表</CardTitle>
                  <CardDescription>
                    全部项目及负责人；admin 创建项目并指定负责人
                  </CardDescription>
                </div>
                <Button onClick={() => setCreateOpen(true)}>
                  <Plus className="size-4" />
                  新建项目
                </Button>
              </CardHeader>
              <CardContent>
                {projectsLoading ? (
                  <div className="space-y-2">
                    <Skeleton className="h-10 w-full" />
                    <Skeleton className="h-10 w-full" />
                  </div>
                ) : (
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>项目</TableHead>
                        <TableHead>负责人</TableHead>
                        <TableHead>描述</TableHead>
                        <TableHead className="text-right">创建时间</TableHead>
                        <TableHead className="text-right">操作</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {(projects ?? []).length === 0 ? (
                        <TableRow>
                          <TableCell
                            colSpan={5}
                            className="text-center text-sm text-muted-foreground"
                          >
                            暂无项目，点击「新建项目」创建
                          </TableCell>
                        </TableRow>
                      ) : (
                        (projects ?? []).map((p) => (
                          <TableRow key={p.id}>
                            <TableCell className="font-medium">
                              {p.name}
                            </TableCell>
                            <TableCell>
                              {p.leader ? (
                                <div>
                                  <div>{p.leader.display_name}</div>
                                  <div className="text-xs text-muted-foreground">
                                    {p.leader.username}
                                  </div>
                                </div>
                              ) : (
                                <span className="text-muted-foreground">—</span>
                              )}
                            </TableCell>
                            <TableCell className="max-w-xs truncate text-muted-foreground">
                              {p.description ?? "—"}
                            </TableCell>
                            <TableCell className="text-right text-muted-foreground">
                              {formatDateTime(p.created_at)}
                            </TableCell>
                            <TableCell className="text-right">
                              <Button
                                variant="ghost"
                                size="sm"
                                onClick={() => setLeaderChangeTarget(p)}
                              >
                                <UserCog className="size-4" />
                                变更负责人
                              </Button>
                            </TableCell>
                          </TableRow>
                        ))
                      )}
                    </TableBody>
                  </Table>
                )}
              </CardContent>
            </Card>
          </TabsContent>

          <TabsContent value="users" className="mt-4">
            <Card>
              <CardHeader className="flex-row items-center justify-between space-y-0">
                <div>
                  <CardTitle>账号管理</CardTitle>
                  <CardDescription>
                    admin 建号；启用/禁用账号（禁用后立即无法登录，不能禁用自己）
                  </CardDescription>
                </div>
                <Button onClick={() => setCreateAccountOpen(true)}>
                  <Plus className="size-4" />
                  新建账号
                </Button>
              </CardHeader>
              <CardContent>
                {usersLoading ? (
                  <div className="space-y-2">
                    <Skeleton className="h-10 w-full" />
                    <Skeleton className="h-10 w-full" />
                  </div>
                ) : (
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>账号</TableHead>
                        <TableHead>管理员</TableHead>
                        <TableHead>状态</TableHead>
                        <TableHead className="text-right">操作</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {(users ?? []).map((u) => (
                        <TableRow key={u.id}>
                          <TableCell>
                            <div className="font-medium">{u.username}</div>
                            <div className="text-xs text-muted-foreground">
                              注册于 {formatDateTime(u.created_at)}
                            </div>
                          </TableCell>
                          <TableCell>
                            {u.is_admin ? (
                              <Badge variant="outline">全局管理员</Badge>
                            ) : (
                              <span className="text-muted-foreground">—</span>
                            )}
                          </TableCell>
                          <TableCell>
                            <Badge
                              variant={u.is_active ? "default" : "destructive"}
                            >
                              {u.is_active ? "启用" : "已禁用"}
                            </Badge>
                          </TableCell>
                          <TableCell className="text-right">
                            {/* 不能禁用自己：后端也会拒绝，UI 先行隐藏避免锁死管理入口 */}
                            {u.id === me?.id ? null : (
                              <Button
                                variant="ghost"
                                size="sm"
                                onClick={() => toggleActive.mutate(u)}
                                disabled={toggleActive.isPending}
                              >
                                {u.is_active ? (
                                  <>
                                    <UserX className="size-4" />
                                    禁用
                                  </>
                                ) : (
                                  <>
                                    <UserCheck className="size-4" />
                                    启用
                                  </>
                                )}
                              </Button>
                            )}
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                )}
              </CardContent>
            </Card>
          </TabsContent>

          <TabsContent value="audit" className="mt-4">
            <Card>
              <CardHeader>
                <CardTitle>审计查看</CardTitle>
                <CardDescription>
                  平台级操作留痕（创建项目、账号启停等），最近 50 条
                </CardDescription>
              </CardHeader>
              <CardContent>
                {!events || events.length === 0 ? (
                  <p className="text-sm text-muted-foreground">暂无事件</p>
                ) : (
                  <ul className="space-y-2">
                    {events.map((e) => (
                      <li
                        key={e.id}
                        className="flex flex-wrap items-center gap-2 text-sm"
                      >
                        <span className="shrink-0 text-muted-foreground">
                          {formatDateTime(e.created_at)}
                        </span>
                        <span className="font-medium">{actorName(e)}</span>
                        <Badge variant="secondary">
                          {ACTION_LABELS[e.action] ?? e.action}
                        </Badge>
                        {e.target_type ? (
                          <span className="text-muted-foreground">
                            {TARGET_TYPE_LABELS[e.target_type] ??
                              e.target_type}
                          </span>
                        ) : null}
                      </li>
                    ))}
                  </ul>
                )}
              </CardContent>
            </Card>
          </TabsContent>
        </Tabs>
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
    </div>
  );
}
