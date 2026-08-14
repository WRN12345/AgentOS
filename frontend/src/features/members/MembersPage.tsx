import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Pencil, Plus, UserX, UserCheck } from "lucide-react";
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
import { api, errorMessage, newIdempotencyKey } from "../../services/api";
import { useAuthStore, useCanManageMembers } from "../../app/store";
import { roleBadgeVariant, roleLabel } from "../../lib/roles";
import type { Member, MemberWithPassword } from "../../types";
import {
  CapabilitiesDialog,
  CreateMemberDialog,
  EditMemberDialog,
  InitialPasswordDialog,
} from "./member-dialogs";
import { queryKeys } from "../../lib/queryKeys";

/** 成员与能力页：全员摘要列表；负责人/管理员可创建/编辑/禁用/确认能力，成员可填报自己的能力。 */
export default function MembersPage() {
  const queryClient = useQueryClient();
  const canManage = useCanManageMembers();
  const selfMember = useAuthStore((s) => s.member);

  const [createOpen, setCreateOpen] = useState(false);
  const [created, setCreated] = useState<MemberWithPassword | null>(null);
  const [editing, setEditing] = useState<Member | null>(null);
  const [capEditing, setCapEditing] = useState<Member | null>(null);

  const { data: members, isLoading } = useQuery({
    queryKey: queryKeys.members(),
    queryFn: () => api.get<Member[]>("/members"),
  });

  // 负责人/管理员禁用/启用成员
  const toggleActive = useMutation({
    mutationFn: (member: Member) =>
      api.patch<Member>(
        `/members/${member.id}`,
        { is_active: !member.is_active },
        newIdempotencyKey(),
      ),
    onSuccess: (updated) => {
      toast.success(
        updated.is_active
          ? `已启用成员 ${updated.display_name}`
          : `已禁用成员 ${updated.display_name}`,
      );
      queryClient.invalidateQueries({ queryKey: queryKeys.members() });
    },
    onError: (error) => toast.error(errorMessage(error, "操作失败")),
  });

  // 负责人/管理员确认某条能力（回填全量能力并携带 confirm）
  const confirmCapability = useMutation({
    mutationFn: (member: Member) =>
      api.put<Member>(
        `/members/${member.id}/capabilities`,
        {
          capabilities: member.capabilities.map((c) => ({
            tag: c.tag,
            proficiency: c.proficiency,
          })),
          confirm: true,
        },
        newIdempotencyKey(),
      ),
    onSuccess: (updated) => {
      toast.success(`已确认 ${updated.display_name} 的能力`);
      queryClient.invalidateQueries({ queryKey: queryKeys.members() });
    },
    onError: (error) => toast.error(errorMessage(error, "确认能力失败")),
  });

  // 优先使用查询返回的最新成员数据（store 中的可能滞后）
  const freshSelf =
    members?.find((m) => m.id === selfMember?.id) ?? selfMember;

  // 管理员为全局角色、不再是成员，列表无需按角色过滤
  const visibleMembers = members ?? [];

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between space-y-0">
        <div>
          <CardTitle>成员与能力</CardTitle>
          <CardDescription>
            团队成员的角色、技能与工作量；AI 分配任务时参考这里的能力数据
          </CardDescription>
        </div>
        <div className="flex gap-2">
          {freshSelf && (
            <Button
              variant="outline"
              onClick={() => setCapEditing(freshSelf)}
            >
              填报我的能力
            </Button>
          )}
          {canManage && (
            <Button onClick={() => setCreateOpen(true)}>
              <Plus className="size-4" />
              新建成员
            </Button>
          )}
        </div>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <div className="space-y-2">
            <Skeleton className="h-10 w-full" />
            <Skeleton className="h-10 w-full" />
            <Skeleton className="h-10 w-full" />
          </div>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>成员</TableHead>
                <TableHead>角色</TableHead>
                <TableHead>能力标签</TableHead>
                <TableHead className="text-right">活跃任务</TableHead>
                <TableHead className="text-right">每周可投入</TableHead>
                <TableHead>Git 用户名</TableHead>
                <TableHead>状态</TableHead>
                {canManage && <TableHead className="text-right">操作</TableHead>}
              </TableRow>
            </TableHeader>
            <TableBody>
              {visibleMembers.map((m) => {
                const hasUnconfirmed = m.capabilities.some(
                  (c) => !c.confirmed,
                );
                const canOperateRow = canManage;
                return (
                  <TableRow key={m.id}>
                    <TableCell>
                      <div className="font-medium">{m.display_name}</div>
                      <div className="text-xs text-muted-foreground">
                        {m.username}
                      </div>
                    </TableCell>
                    <TableCell>
                      <Badge variant={roleBadgeVariant(m.role)}>
                        {roleLabel(m.role)}
                      </Badge>
                    </TableCell>
                    <TableCell>
                      <div className="flex max-w-xs flex-wrap gap-1">
                        {m.capabilities.length === 0 && (
                          <span className="text-xs text-muted-foreground">
                            未填报
                          </span>
                        )}
                        {m.capabilities.map((c) => (
                          <Badge
                            key={c.id}
                            variant={c.confirmed ? "default" : "outline"}
                            title={c.confirmed ? "已确认" : "待确认"}
                          >
                            {c.tag} {c.proficiency}
                            {!c.confirmed && "（待确认）"}
                          </Badge>
                        ))}
                      </div>
                    </TableCell>
                    <TableCell className="text-right">
                      {m.active_work_items}
                    </TableCell>
                    <TableCell className="text-right">
                      {m.weekly_available_hours != null
                        ? `${m.weekly_available_hours}h`
                        : "—"}
                    </TableCell>
                    <TableCell>{m.git_username ?? "—"}</TableCell>
                    <TableCell>
                      <Badge variant={m.is_active ? "default" : "destructive"}>
                        {m.is_active ? "启用" : "已禁用"}
                      </Badge>
                    </TableCell>
                    {canManage && (
                      <TableCell className="text-right">
                        {canOperateRow && (
                        <div className="flex justify-end gap-1">
                          {hasUnconfirmed && (
                            <Button
                              variant="ghost"
                              size="sm"
                              onClick={() => confirmCapability.mutate(m)}
                              disabled={confirmCapability.isPending}
                            >
                              <Check className="size-4" />
                              确认能力
                            </Button>
                          )}
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={() => setEditing(m)}
                          >
                            <Pencil className="size-4" />
                            编辑
                          </Button>
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={() => toggleActive.mutate(m)}
                            disabled={toggleActive.isPending}
                          >
                            {m.is_active ? (
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
                        </div>
                        )}
                      </TableCell>
                    )}
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        )}
      </CardContent>

      <CreateMemberDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        onCreated={setCreated}
      />
      <InitialPasswordDialog member={created} onClose={() => setCreated(null)} />
      <EditMemberDialog member={editing} onClose={() => setEditing(null)} />
      <CapabilitiesDialog
        member={capEditing}
        onClose={() => setCapEditing(null)}
      />
    </Card>
  );
}
