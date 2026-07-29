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

const createSchema = z.object({
  username: z.string().min(1, "请输入用户名"),
  password: z.string().min(8, "密码至少 8 位"),
  display_name: z.string().min(1, "请输入显示名"),
  role: z.enum(["leader", "member", "admin"]),
  weekly_available_hours: z.string().optional(),
  git_username: z.string().optional(),
});

type CreateValues = z.infer<typeof createSchema>;

interface CreateMemberDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** 创建成功后回调，用于展示一次性初始密码。 */
  onCreated: (member: MemberWithPassword) => void;
}

/** 负责人/管理员创建成员对话框：成功后后端返回一次性初始密码。 */
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
      password: "",
      display_name: "",
      role: "member",
      weekly_available_hours: "",
      git_username: "",
    },
  });

  const mutation = useMutation({
    mutationFn: (values: CreateValues) =>
      api.post<MemberWithPassword>(
        "/members",
        {
          username: values.username,
          password: values.password,
          display_name: values.display_name,
          role: values.role,
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
      toast.success(`成员 ${member.display_name} 创建成功`);
      queryClient.invalidateQueries({ queryKey: ["members"] });
      form.reset();
      onOpenChange(false);
      onCreated(member);
    },
    onError: (error) => toast.error(errorMessage(error, "创建成员失败")),
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>新建成员</DialogTitle>
          <DialogDescription>
            创建成员账号，初始密码仅展示一次，请转交本人。
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
              name="password"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>初始密码</FormLabel>
                  <FormControl>
                    <Input
                      type="password"
                      autoComplete="new-password"
                      {...field}
                    />
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
              name="role"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>角色</FormLabel>
                  <Select value={field.value} onValueChange={field.onChange}>
                    <FormControl>
                      <SelectTrigger>
                        <SelectValue />
                      </SelectTrigger>
                    </FormControl>
                    <SelectContent>
                      <SelectItem value="member">成员</SelectItem>
                      <SelectItem value="leader">负责人</SelectItem>
                      <SelectItem value="admin">管理员</SelectItem>
                    </SelectContent>
                  </Select>
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
                {mutation.isPending ? "创建中…" : "创建"}
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
  role: z.enum(["leader", "member", "admin"]),
  weekly_available_hours: z.string().optional(),
  git_username: z.string().optional(),
});

type EditValues = z.infer<typeof editSchema>;

interface EditMemberDialogProps {
  member: Member | null;
  onClose: () => void;
}

/** 负责人/管理员编辑成员对话框：显示名、角色、可投入时间与 Git 用户名。 */
export function EditMemberDialog({ member, onClose }: EditMemberDialogProps) {
  const queryClient = useQueryClient();
  const form = useForm<EditValues>({
    resolver: zodResolver(editSchema),
    values: member
      ? {
          display_name: member.display_name,
          role: member.role,
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
          role: values.role,
          weekly_available_hours: values.weekly_available_hours
            ? Number(values.weekly_available_hours)
            : null,
          git_username: values.git_username || null,
        },
        newIdempotencyKey(),
      ),
    onSuccess: (updated) => {
      toast.success(`已更新成员 ${updated.display_name}`);
      queryClient.invalidateQueries({ queryKey: ["members"] });
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
              name="role"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>角色</FormLabel>
                  <Select value={field.value} onValueChange={field.onChange}>
                    <FormControl>
                      <SelectTrigger>
                        <SelectValue />
                      </SelectTrigger>
                    </FormControl>
                    <SelectContent>
                      <SelectItem value="member">成员</SelectItem>
                      <SelectItem value="leader">负责人</SelectItem>
                      <SelectItem value="admin">管理员</SelectItem>
                    </SelectContent>
                  </Select>
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
      queryClient.invalidateQueries({ queryKey: ["members"] });
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

interface InitialPasswordDialogProps {
  member: MemberWithPassword | null;
  onClose: () => void;
}

/** 创建成功后展示一次性初始密码的对话框。 */
export function InitialPasswordDialog({
  member,
  onClose,
}: InitialPasswordDialogProps) {
  const [copied, setCopied] = useState(false);
  return (
    <Dialog open={member !== null} onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>成员创建成功</DialogTitle>
          <DialogDescription>
            初始密码仅展示一次，请立即复制并转交 {member?.display_name}。
          </DialogDescription>
        </DialogHeader>
        <div className="rounded-md bg-muted p-4 text-center font-mono text-lg">
          {member?.initial_password}
        </div>
        <DialogFooter>
          <Button
            variant="outline"
            onClick={() => {
              if (member) {
                navigator.clipboard.writeText(member.initial_password);
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
