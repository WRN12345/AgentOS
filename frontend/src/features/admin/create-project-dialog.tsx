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
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from "@/components/ui/form";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { api, errorMessage, newIdempotencyKey } from "../../services/api";
import { queryKeys } from "../../lib/queryKeys";
import { LeaderUsernameField } from "./leader-username-field";
import { resolveUser } from "./resolve-user";
import type { AdminProject, UserMe } from "../../types";

const createSchema = z.object({
  name: z.string().min(1, "请输入项目名称").max(128),
  description: z.string().max(1000).optional(),
  owner_username: z.string().min(1, "请输入负责人用户名"),
});

type CreateValues = z.infer<typeof createSchema>;

interface CreateProjectDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

/**
 * 管理控制台"新建项目"对话框（ticket 10）：
 * admin 输入完整用户名解析指定负责人（无搜索端点），创建后该负责人成为项目的 leader 成员。
 */
export function CreateProjectDialog({
  open,
  onOpenChange,
}: CreateProjectDialogProps) {
  const queryClient = useQueryClient();
  const { data: users } = useQuery({
    queryKey: queryKeys.adminUsers(),
    queryFn: () => api.get<UserMe[]>("/users"),
  });

  const form = useForm<CreateValues>({
    resolver: zodResolver(createSchema),
    defaultValues: { name: "", description: "", owner_username: "" },
  });

  const mutation = useMutation({
    mutationFn: (values: CreateValues & { owner_user_id: string }) =>
      api.post<AdminProject>(
        "/projects",
        {
          name: values.name,
          description: values.description || null,
          owner_user_id: values.owner_user_id,
        },
        newIdempotencyKey(),
      ),
    onSuccess: (project) => {
      toast.success(`项目 ${project.name} 创建成功，负责人可进入工作台`);
      // 项目列表 + 平台审计（project.created）一并失效
      queryClient.invalidateQueries({ queryKey: queryKeys.adminProjects() });
      queryClient.invalidateQueries({ queryKey: queryKeys.adminAuditEvents() });
      form.reset();
      onOpenChange(false);
    },
    onError: (error) => toast.error(errorMessage(error, "创建项目失败")),
  });

  const onSubmit = (values: CreateValues) => {
    const { resolved } = resolveUser(users, values.owner_username);
    if (!resolved) {
      form.setError("owner_username", {
        type: "manual",
        message: "请填写可担任负责人的已有账号（非 admin、已启用）",
      });
      return;
    }
    mutation.mutate({ ...values, owner_user_id: resolved.id });
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>新建项目</DialogTitle>
          <DialogDescription>
            创建项目并指定负责人，负责人将立即获得该项目工作台访问权。
          </DialogDescription>
        </DialogHeader>
        <Form {...form}>
          <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-4">
            <FormField
              control={form.control}
              name="name"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>项目名称</FormLabel>
                  <FormControl>
                    <Input {...field} />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />
            <FormField
              control={form.control}
              name="description"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>项目描述（可选）</FormLabel>
                  <FormControl>
                    <Textarea rows={3} {...field} />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />
            <LeaderUsernameField
              control={form.control}
              name="owner_username"
              label="负责人用户名"
              users={users}
            />
            <DialogFooter>
              <Button type="submit" disabled={mutation.isPending}>
                {mutation.isPending ? "创建中…" : "创建项目"}
              </Button>
            </DialogFooter>
          </form>
        </Form>
      </DialogContent>
    </Dialog>
  );
}
