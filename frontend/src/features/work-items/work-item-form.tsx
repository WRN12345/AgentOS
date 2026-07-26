import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
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
import {
  api,
  ApiError,
  errorMessage,
  newIdempotencyKey,
  VERSION_CONFLICT_MESSAGE,
} from "../../services/api";
import type { Member, WorkItem } from "../../types";

const formSchema = z.object({
  title: z.string().min(1, "请输入标题"),
  description: z.string().optional(),
  acceptance_criteria: z.string().optional(),
  priority: z.enum(["low", "medium", "high", "urgent"]),
  assignee_id: z.string().min(1, "请选择主执行人"),
  collaborator_ids: z.array(z.string()),
  due_at: z.string().optional(),
});

type FormValues = z.infer<typeof formSchema>;

/** 将 <input type="date"> 的本地日期值转为 ISO 字符串（空值转为 null）。 */
function toIsoDate(value: string | undefined): string | null {
  if (!value) return null;
  return new Date(`${value}T00:00:00`).toISOString();
}

/** 将后端 ISO 时间转为 <input type="date"> 的值。 */
function toDateInput(value: string | null | undefined): string {
  if (!value) return "";
  const d = new Date(value);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

interface WorkItemFormDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  members: Member[];
  /** 传入则为编辑模式（负责人 PATCH，需携带当前 version）。 */
  workItem?: WorkItem | null;
}

/** 负责人创建/编辑工作项对话框。 */
export function WorkItemFormDialog({
  open,
  onOpenChange,
  members,
  workItem,
}: WorkItemFormDialogProps) {
  const queryClient = useQueryClient();
  const isEdit = Boolean(workItem);

  const form = useForm<FormValues>({
    resolver: zodResolver(formSchema),
    values: workItem
      ? {
          title: workItem.title,
          description: workItem.description ?? "",
          acceptance_criteria: workItem.acceptance_criteria ?? "",
          priority: workItem.priority,
          assignee_id: workItem.assignee.id,
          collaborator_ids: workItem.collaborators.map((c) => c.id),
          due_at: toDateInput(workItem.due_at),
        }
      : {
          title: "",
          description: "",
          acceptance_criteria: "",
          priority: "medium",
          assignee_id: "",
          collaborator_ids: [],
          due_at: "",
        },
  });

  const mutation = useMutation({
    mutationFn: (values: FormValues) => {
      const payload = {
        title: values.title,
        description: values.description || null,
        acceptance_criteria: values.acceptance_criteria || null,
        priority: values.priority,
        assignee_id: values.assignee_id,
        collaborator_ids: values.collaborator_ids,
        due_at: toIsoDate(values.due_at),
      };
      if (isEdit && workItem) {
        // 编辑走乐观锁：携带当前版本号
        return api.patch<WorkItem>(
          `/work-items/${workItem.id}`,
          { version: workItem.version, ...payload },
          newIdempotencyKey(),
        );
      }
      return api.post<WorkItem>("/work-items", payload, newIdempotencyKey());
    },
    onSuccess: () => {
      toast.success(isEdit ? "工作项已更新" : "工作项已创建");
      queryClient.invalidateQueries({ queryKey: ["work-items"] });
      form.reset();
      onOpenChange(false);
    },
    onError: (error) => {
      if (error instanceof ApiError && error.isVersionConflict) {
        toast.error(VERSION_CONFLICT_MESSAGE);
        queryClient.invalidateQueries({ queryKey: ["work-items"] });
        return;
      }
      toast.error(errorMessage(error, "保存工作项失败"));
    },
  });

  const activeMembers = members.filter((m) => m.is_active);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] max-w-xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{isEdit ? "编辑工作项" : "创建工作项"}</DialogTitle>
          <DialogDescription>
            {isEdit
              ? "修改后保存，版本冲突时请先刷新数据。"
              : "创建后处于草稿状态，发布后主执行人方可开始。"}
          </DialogDescription>
        </DialogHeader>
        <Form {...form}>
          <form
            onSubmit={form.handleSubmit((v) => mutation.mutate(v))}
            className="space-y-4"
          >
            <FormField
              control={form.control}
              name="title"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>标题</FormLabel>
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
                  <FormLabel>说明（可选）</FormLabel>
                  <FormControl>
                    <Textarea rows={3} {...field} />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />
            <FormField
              control={form.control}
              name="acceptance_criteria"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>验收标准（可选）</FormLabel>
                  <FormControl>
                    <Textarea rows={3} {...field} />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />
            <div className="grid grid-cols-2 gap-4">
              <FormField
                control={form.control}
                name="priority"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>优先级</FormLabel>
                    <Select value={field.value} onValueChange={field.onChange}>
                      <FormControl>
                        <SelectTrigger>
                          <SelectValue />
                        </SelectTrigger>
                      </FormControl>
                      <SelectContent>
                        <SelectItem value="low">低</SelectItem>
                        <SelectItem value="medium">中</SelectItem>
                        <SelectItem value="high">高</SelectItem>
                        <SelectItem value="urgent">紧急</SelectItem>
                      </SelectContent>
                    </Select>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <FormField
                control={form.control}
                name="due_at"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>截止时间（可选）</FormLabel>
                    <FormControl>
                      <Input type="date" {...field} />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
            </div>
            <FormField
              control={form.control}
              name="assignee_id"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>主执行人</FormLabel>
                  <Select value={field.value} onValueChange={field.onChange}>
                    <FormControl>
                      <SelectTrigger>
                        <SelectValue placeholder="选择成员" />
                      </SelectTrigger>
                    </FormControl>
                    <SelectContent>
                      {activeMembers.map((m) => (
                        <SelectItem key={m.id} value={m.id}>
                          {m.display_name}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  <FormMessage />
                </FormItem>
              )}
            />
            <FormField
              control={form.control}
              name="collaborator_ids"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>协作者（可多选）</FormLabel>
                  <div className="grid grid-cols-2 gap-2 rounded-md border p-3">
                    {activeMembers.map((m) => {
                      const checked = field.value.includes(m.id);
                      return (
                        <label
                          key={m.id}
                          className="flex items-center gap-2 text-sm"
                        >
                          <Checkbox
                            checked={checked}
                            onCheckedChange={(v) => {
                              field.onChange(
                                v === true
                                  ? [...field.value, m.id]
                                  : field.value.filter((id) => id !== m.id),
                              );
                            }}
                          />
                          {m.display_name}
                        </label>
                      );
                    })}
                  </div>
                  <FormMessage />
                </FormItem>
              )}
            />
            <DialogFooter>
              <Button type="submit" disabled={mutation.isPending}>
                {mutation.isPending
                  ? "保存中…"
                  : isEdit
                    ? "保存"
                    : "创建"}
              </Button>
            </DialogFooter>
          </form>
        </Form>
      </DialogContent>
    </Dialog>
  );
}
