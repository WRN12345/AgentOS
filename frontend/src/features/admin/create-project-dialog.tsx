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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { api, errorMessage, newIdempotencyKey } from "../../services/api";
import { queryKeys } from "../../lib/queryKeys";
import type { AdminProject, UserMe } from "../../types";

const createSchema = z.object({
  name: z.string().min(1, "请输入项目名称").max(128),
  description: z.string().max(1000).optional(),
  owner_user_id: z.string().min(1, "请选择负责人"),
});

type CreateValues = z.infer<typeof createSchema>;

interface CreateProjectDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

/**
 * 管理控制台"新建项目"对话框（ticket 10）：
 * admin 指定负责人，创建后该负责人成为项目的 leader 成员，可立即进入项目工作台。
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

  // 负责人只能是普通启用用户（全局管理员不参与项目业务，16 节）
  const ownerCandidates = (users ?? []).filter(
    (u) => !u.is_admin && u.is_active,
  );

  const form = useForm<CreateValues>({
    resolver: zodResolver(createSchema),
    defaultValues: { name: "", description: "", owner_user_id: "" },
  });

  const mutation = useMutation({
    mutationFn: (values: CreateValues) =>
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
      queryClient.invalidateQueries({ queryKey: ["audit-events"] });
      form.reset();
      onOpenChange(false);
    },
    onError: (error) => toast.error(errorMessage(error, "创建项目失败")),
  });

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
          <form
            onSubmit={form.handleSubmit((v) => mutation.mutate(v))}
            className="space-y-4"
          >
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
            <FormField
              control={form.control}
              name="owner_user_id"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>负责人</FormLabel>
                  <Select value={field.value} onValueChange={field.onChange}>
                    <FormControl>
                      <SelectTrigger>
                        <SelectValue placeholder="选择负责人账号" />
                      </SelectTrigger>
                    </FormControl>
                    <SelectContent>
                      {ownerCandidates.length === 0 && (
                        <div className="px-2 py-1.5 text-sm text-muted-foreground">
                          暂无可用账号
                        </div>
                      )}
                      {ownerCandidates.map((u) => (
                        <SelectItem key={u.id} value={u.id}>
                          {u.username}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  <FormMessage />
                </FormItem>
              )}
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
