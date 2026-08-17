import { useState } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { useMutation, useQueryClient } from "@tanstack/react-query";
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
import { api, errorMessage, newIdempotencyKey } from "../../services/api";
import { queryKeys } from "../../lib/queryKeys";
import type { CreatedAccount } from "../../types";

const createSchema = z.object({
  username: z.string().min(1, "请输入用户名").max(64),
  password: z.string().min(8, "密码至少 8 位").max(128),
});

type CreateValues = z.infer<typeof createSchema>;

interface CreateAccountDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

/**
 * 管理控制台"新建账号"对话框（建号收敛到 admin，16 节）：
 * admin 创建全局账号，一次性初始密码仅此一次返回，之后不可再查。
 */
export function CreateAccountDialog({
  open,
  onOpenChange,
}: CreateAccountDialogProps) {
  const queryClient = useQueryClient();
  const [created, setCreated] = useState<CreatedAccount | null>(null);
  const [copied, setCopied] = useState(false);

  const form = useForm<CreateValues>({
    resolver: zodResolver(createSchema),
    defaultValues: { username: "", password: "" },
  });

  const mutation = useMutation({
    mutationFn: (values: CreateValues) =>
      api.post<CreatedAccount>(
        "/users",
        { username: values.username, password: values.password },
        newIdempotencyKey(),
      ),
    onSuccess: (account) => {
      toast.success(`账号 ${account.username} 创建成功`);
      queryClient.invalidateQueries({ queryKey: queryKeys.adminUsers() });
      queryClient.invalidateQueries({ queryKey: ["audit-events"] });
      form.reset();
      setCreated(account);
      setCopied(false);
    },
    onError: (error) => toast.error(errorMessage(error, "创建账号失败")),
  });

  const close = () => {
    setCreated(null);
    setCopied(false);
    onOpenChange(false);
  };

  return (
    <Dialog open={open} onOpenChange={(o) => !o && close()}>
      <DialogContent className="sm:max-w-md">
        {created === null ? (
          <>
            <DialogHeader>
              <DialogTitle>新建账号</DialogTitle>
              <DialogDescription>
                创建全局账号，初始密码仅展示一次，请转交本人并提醒尽快修改。
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
                <DialogFooter>
                  <Button type="submit" disabled={mutation.isPending}>
                    {mutation.isPending ? "创建中…" : "创建账号"}
                  </Button>
                </DialogFooter>
              </form>
            </Form>
          </>
        ) : (
          <>
            <DialogHeader>
              <DialogTitle>账号创建成功</DialogTitle>
              <DialogDescription>
                初始密码仅展示一次，请立即复制并转交 {created.username}。
              </DialogDescription>
            </DialogHeader>
            <div className="rounded-md bg-muted p-4 text-center font-mono text-lg">
              {created.initial_password}
            </div>
            <DialogFooter>
              <Button
                variant="outline"
                onClick={() => {
                  void navigator.clipboard.writeText(created.initial_password);
                  setCopied(true);
                }}
              >
                {copied ? "已复制" : "复制密码"}
              </Button>
              <Button onClick={close}>我已保存</Button>
            </DialogFooter>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
