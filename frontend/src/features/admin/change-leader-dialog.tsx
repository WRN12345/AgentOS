import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Form,
} from "@/components/ui/form";
import { api, errorMessage, newIdempotencyKey } from "../../services/api";
import { queryKeys } from "../../lib/queryKeys";
import { LeaderUsernameField } from "./leader-username-field";
import { resolveUser } from "./resolve-user";
import type { AdminProject, UserMe } from "../../types";

const changeSchema = z.object({
  leader_username: z.string().min(1, "请输入负责人用户名"),
});

type ChangeValues = z.infer<typeof changeSchema>;

interface ChangeLeaderDialogProps {
  project: AdminProject | null;
  onClose: () => void;
}

/**
 * 管理控制台"变更负责人"对话框（每项目仅一名负责人，仅 admin 指定/变更）：
 * 输入完整用户名解析目标账号，原负责人自动降为成员（PUT /projects/{id}/leader）。
 */
export function ChangeLeaderDialog({
  project,
  onClose,
}: ChangeLeaderDialogProps) {
  const queryClient = useQueryClient();
  const { data: users } = useQuery({
    queryKey: queryKeys.adminUsers(),
    queryFn: () => api.get<UserMe[]>("/users"),
  });

  const form = useForm<ChangeValues>({
    resolver: zodResolver(changeSchema),
    values: { leader_username: project?.leader?.username ?? "" },
  });

  const mutation = useMutation({
    mutationFn: (values: ChangeValues & { user_id: string }) =>
      api.put<AdminProject>(
        `/projects/${project!.id}/leader`,
        { user_id: values.user_id },
        newIdempotencyKey(),
      ),
    onSuccess: (updated) => {
      toast.success(
        `项目 ${updated.name} 负责人已变更为 ${updated.leader?.display_name}`,
      );
      // 项目列表 + 平台审计（project.leader.updated）一并失效
      queryClient.invalidateQueries({ queryKey: queryKeys.adminProjects() });
      queryClient.invalidateQueries({ queryKey: ["audit-events"] });
      onClose();
    },
    onError: (error) => toast.error(errorMessage(error, "变更负责人失败")),
  });

  const onSubmit = (values: ChangeValues) => {
    const { resolved } = resolveUser(users, values.leader_username);
    if (!resolved) {
      form.setError("leader_username", {
        type: "manual",
        message: "请填写可担任负责人的已有账号（非 admin、已启用）",
      });
      return;
    }
    mutation.mutate({ ...values, user_id: resolved.id });
  };

  return (
    <Dialog open={project !== null} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>变更负责人：{project?.name}</DialogTitle>
          <DialogDescription>
            {project?.leader
              ? `当前负责人 ${project.leader.display_name}（${project.leader.username}），变更后自动降为普通成员。`
              : "该项目尚无负责人，请指定一名负责人。"}
          </DialogDescription>
        </DialogHeader>
        <Form {...form}>
          <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-4">
            <LeaderUsernameField
              control={form.control}
              name="leader_username"
              label="新负责人用户名"
              users={users}
            />
            <DialogFooter>
              <Button type="submit" disabled={mutation.isPending}>
                {mutation.isPending ? "变更中…" : "变更负责人"}
              </Button>
            </DialogFooter>
          </form>
        </Form>
      </DialogContent>
    </Dialog>
  );
}
