import { useState } from "react";
import { useForm, useFieldArray } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Plus, Trash2 } from "lucide-react";
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { api, errorMessage, newIdempotencyKey } from "../../services/api";
import { useAuthStore } from "../../app/store";
import type { Member, MemberWithPassword } from "../../types";
import { queryKeys } from "../../lib/queryKeys";

const createSchema = z.object({
  username: z.string().min(1, "请输入用户名"),
  display_name: z.string().optional(),
  weekly_available_hours: z.string().optional(),
  git_username: z.string().optional(),
});

type CreateValues = z.infer<typeof createSchema>;

interface CreateMemberDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** 添加成功后回调；复用已有账号、无初始密码（initial_password 恒为 null）。 */
  onCreated: (member: MemberWithPassword) => void;
}

/**
 * 负责人添加已有账号成员对话框（建号收敛到 admin，16 节）：
 * 按全局唯一用户名解析已有账号加入本项目，不建号、无初始密码、固定为「成员」角色。
 */
export function CreateMemberDialog({
  open,
  onOpenChange,
  onCreated,
}: CreateMemberDialogProps) {
  const queryClient = useQueryClient();
  const form = useForm<CreateValues>({
    resolver: zodResolver(createSchema),
    defaultValues: {
      username: "",
      display_name: "",
      weekly_available_hours: "",
      git_username: "",
    },
  });

  const mutation = useMutation({
    mutationFn: (values: CreateValues) =>
      api.post<Member>(
        "/members",
        {
          username: values.username,
          ...(values.display_name
            ? { display_name: values.display_name }
            : {}),
          ...(values.weekly_available_hours
            ? { weekly_available_hours: Number(values.weekly_available_hours) }
            : {}),
          ...(values.git_username
            ? { git_username: values.git_username }
            : {}),
        },
        newIdempotencyKey(),
      ),
    onSuccess: (member) => {
      toast.success(`已添加成员 ${member.display_name}`);
      queryClient.invalidateQueries({ queryKey: queryKeys.members() });
      form.reset();
      onOpenChange(false);
      // 复用已有账号，无初始密码（响应本身不含该字段，显式置 null）
      onCreated({ ...member, initial_password: null });
    },
    onError: (error) => toast.error(errorMessage(error, "添加成员失败")),
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>添加已有成员</DialogTitle>
          <DialogDescription>
            按全局唯一用户名将已有账号加入本项目；不新建账号、无初始密码，角色固定为成员。
          </DialogDescription>
        </DialogHeader>
        <Form {...form}>
          <form
            onSubmit={form.handleSubmit((v) => mutation.mutate(v))}
            className="space-y-4"
          >
            <FormField
              control={form.control}
              name="username"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>用户名</FormLabel>
                  <FormControl>
                    <Input autoComplete="off" {...field} />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />
            <FormField
              control={form.control}
              name="display_name"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>显示名（可选，默认取账号用户名）</FormLabel>
                  <FormControl>
                    <Input {...field} />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />
            <FormField
              control={form.control}
              name="weekly_available_hours"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>每周可投入时间（小时，可选）</FormLabel>
                  <FormControl>
                    <Input type="number" min={0} step={1} {...field} />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />
            <FormField
              control={form.control}
              name="git_username"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>Git 用户名（可选）</FormLabel>
                  <FormControl>
                    <Input {...field} />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />
            <DialogFooter>
              <Button type="submit" disabled={mutation.isPending}>
                {mutation.isPending ? "添加中…" : "添加"}
              </Button>
            </DialogFooter>
          </form>
        </Form>
      </DialogContent>
    </Dialog>
  );
}

const editSchema = z.object({
  display_name: z.string().min(1, "请输入显示名"),
  weekly_available_hours: z.string().optional(),
  git_username: z.string().optional(),
});

type EditValues = z.infer<typeof editSchema>;

interface EditMemberDialogProps {
  member: Member | null;
  onClose: () => void;
}

/** 负责人编辑成员对话框：显示名、可投入时间与 Git 用户名（角色仅 admin 指定，不在此维护）。 */
export function EditMemberDialog({ member, onClose }: EditMemberDialogProps) {
  const queryClient = useQueryClient();
  const form = useForm<EditValues>({
    resolver: zodResolver(editSchema),
    values: member
      ? {
          display_name: member.display_name,
          weekly_available_hours:
            member.weekly_available_hours?.toString() ?? "",
          git_username: member.git_username ?? "",
        }
      : undefined,
  });

  const mutation = useMutation({
    mutationFn: (values: EditValues) =>
      api.patch<Member>(
        `/members/${member!.id}`,
        {
          display_name: values.display_name,
          weekly_available_hours: values.weekly_available_hours
            ? Number(values.weekly_available_hours)
            : null,
          git_username: values.git_username || null,
        },
        newIdempotencyKey(),
      ),
    onSuccess: (updated) => {
      toast.success(`已更新成员 ${updated.display_name}`);
      queryClient.invalidateQueries({ queryKey: queryKeys.members() });
      onClose();
    },
    onError: (error) => toast.error(errorMessage(error, "更新成员失败")),
  });

  return (
    <Dialog open={member !== null} onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>编辑成员：{member?.display_name}</DialogTitle>
        </DialogHeader>
        <Form {...form}>
          <form
            onSubmit={form.handleSubmit((v) => mutation.mutate(v))}
            className="space-y-4"
          >
            <FormField
              control={form.control}
              name="display_name"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>显示名</FormLabel>
                  <FormControl>
                    <Input {...field} />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />
            <FormField
              control={form.control}
              name="weekly_available_hours"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>每周可投入时间（小时）</FormLabel>
                  <FormControl>
                    <Input type="number" min={0} step={1} {...field} />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />
            <FormField
              control={form.control}
              name="git_username"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>Git 用户名</FormLabel>
                  <FormControl>
                    <Input {...field} />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />
            <DialogFooter>
              <Button type="submit" disabled={mutation.isPending}>
                {mutation.isPending ? "保存中…" : "保存"}
              </Button>
            </DialogFooter>
          </form>
        </Form>
      </DialogContent>
    </Dialog>
  );
}

const capabilitySchema = z.object({
  capabilities: z.array(
    z.object({
      tag: z.string().min(1, "请输入能力标签"),
      proficiency: z.string(),
    }),
  ),
});

type CapabilityValues = z.infer<typeof capabilitySchema>;

interface CapabilitiesDialogProps {
  member: Member | null;
  onClose: () => void;
}

/** 成员本人填报能力对话框：能力标签 + 熟练度（1-5），提交后回到未确认状态。 */
export function CapabilitiesDialog({
  member,
  onClose,
}: CapabilitiesDialogProps) {
  const queryClient = useQueryClient();
  const form = useForm<CapabilityValues>({
    resolver: zodResolver(capabilitySchema),
    values: {
      capabilities: (member?.capabilities ?? []).map((c) => ({
        tag: c.tag,
        proficiency: String(c.proficiency),
      })),
    },
  });
  const { fields, append, remove } = useFieldArray({
    control: form.control,
    name: "capabilities",
  });

  const mutation = useMutation({
    mutationFn: (values: CapabilityValues) =>
      api.put<Member>(
        `/members/${member!.id}/capabilities`,
        {
          capabilities: values.capabilities.map((c) => ({
            tag: c.tag,
            proficiency: Number(c.proficiency),
          })),
        },
        newIdempotencyKey(),
      ),
    onSuccess: (updated) => {
      toast.success("能力已提交，等待负责人确认");
      // 同步 store 中的本人成员记录，保持徽标与能力展示一致
      const auth = useAuthStore.getState();
      if (auth.member?.id === updated.id) {
        auth.setMember(updated);
      }
      queryClient.invalidateQueries({ queryKey: queryKeys.members() });
      onClose();
    },
    onError: (error) => toast.error(errorMessage(error, "提交能力失败")),
  });

  return (
    <Dialog open={member !== null} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>填报我的能力</DialogTitle>
          <DialogDescription>
            维护自己的能力标签与熟练度（1-5），提交后需负责人确认。
          </DialogDescription>
        </DialogHeader>
        <Form {...form}>
          <form
            onSubmit={form.handleSubmit((v) => mutation.mutate(v))}
            className="space-y-4"
          >
            <div className="max-h-80 space-y-3 overflow-y-auto pr-1">
              {fields.map((field, index) => (
                <div key={field.id} className="flex items-start gap-2">
                  <FormField
                    control={form.control}
                    name={`capabilities.${index}.tag`}
                    render={({ field }) => (
                      <FormItem className="flex-1">
                        <FormControl>
                          <Input placeholder="能力标签，如 FastAPI" {...field} />
                        </FormControl>
                        <FormMessage />
                      </FormItem>
                    )}
                  />
                  <FormField
                    control={form.control}
                    name={`capabilities.${index}.proficiency`}
                    render={({ field }) => (
                      <FormItem className="w-28">
                        <Select
                          value={field.value}
                          onValueChange={field.onChange}
                        >
                          <FormControl>
                            <SelectTrigger>
                              <SelectValue />
                            </SelectTrigger>
                          </FormControl>
                          <SelectContent>
                            {[1, 2, 3, 4, 5].map((n) => (
                              <SelectItem key={n} value={String(n)}>
                                {n} 级
                              </SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                        <FormMessage />
                      </FormItem>
                    )}
                  />
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon"
                    onClick={() => remove(index)}
                  >
                    <Trash2 className="size-4" />
                  </Button>
                </div>
              ))}
            </div>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => append({ tag: "", proficiency: "3" })}
            >
              <Plus className="size-4" />
              添加能力
            </Button>
            <DialogFooter>
              <Button type="submit" disabled={mutation.isPending}>
                {mutation.isPending ? "提交中…" : "提交"}
              </Button>
            </DialogFooter>
          </form>
        </Form>
      </DialogContent>
    </Dialog>
  );
}

interface MemberAddResultDialogProps {
  member: MemberWithPassword | null;
  onClose: () => void;
}

/** 添加成员结果对话框：复用已有账号时无初始密码（一次性密码由 admin 控制台建号时展示）。 */
export function MemberAddResultDialog({
  member,
  onClose,
}: MemberAddResultDialogProps) {
  const [copied, setCopied] = useState(false);
  const password = member?.initial_password ?? null;
  return (
    <Dialog open={member !== null} onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>
            {password === null ? "已添加成员" : "成员创建成功"}
          </DialogTitle>
          <DialogDescription>
            {password === null
              ? "该账号为复用已有账号，无需初始密码。"
              : `初始密码仅展示一次，请立即复制并转交 ${member?.display_name}。`}
          </DialogDescription>
        </DialogHeader>
        <div className="rounded-md bg-muted p-4 text-center font-mono text-lg">
          {password ?? "（复用已有账号，无初始密码）"}
        </div>
        <DialogFooter>
          <Button
            variant="outline"
            disabled={password === null}
            onClick={() => {
              if (password !== null) {
                navigator.clipboard.writeText(password);
                setCopied(true);
              }
            }}
          >
            {copied ? "已复制" : "复制密码"}
          </Button>
          <Button onClick={onClose}>我已保存</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
