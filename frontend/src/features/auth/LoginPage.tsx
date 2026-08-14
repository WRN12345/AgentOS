import { useState } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Form,
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from "@/components/ui/form";
import { Input } from "@/components/ui/input";
import { api, ApiError } from "../../services/api";
import { useAuthStore } from "../../app/store";
import {
  loadIdentity,
  loadProjects,
  pickRememberedProject,
  selectProject,
} from "./session";
import type { TokenPair } from "../../types";

const loginSchema = z.object({
  username: z.string().min(1, "请输入用户名"),
  password: z.string().min(1, "请输入密码"),
});

type LoginValues = z.infer<typeof loginSchema>;

/** 登录页：shadcn Form（react-hook-form + zod），成功后写入令牌并加载身份。 */
export default function LoginPage() {
  const navigate = useNavigate();
  const setTokens = useAuthStore((s) => s.setTokens);
  const [submitting, setSubmitting] = useState(false);

  const form = useForm<LoginValues>({
    resolver: zodResolver(loginSchema),
    defaultValues: { username: "", password: "" },
  });

  const onSubmit = async (values: LoginValues) => {
    setSubmitting(true);
    try {
      const tokens = await api.post<TokenPair>("/auth/login", values);
      setTokens(tokens);
      // 分流前先记住上次登录持久化的项目选择（loadProjects 会清空 currentProject）
      const prev = useAuthStore.getState();
      const rememberedProject = prev.currentProject;
      const rememberedAt = prev.projectSelectedAt;

      // 拉项目列表（含清空旧上下文），再加载用户身份决定分流
      const projects = await loadProjects();
      await loadIdentity();
      toast.success("登录成功");

      // 管理员：进管理控制台，不参与业务项目
      if (useAuthStore.getState().user?.is_admin) {
        navigate("/console", { replace: true });
        return;
      }
      // 普通用户：24h 记忆窗口内直接进入上次项目，否则进项目选择页
      const remembered = pickRememberedProject(
        projects,
        rememberedProject,
        rememberedAt,
      );
      if (remembered) {
        await selectProject(remembered);
        navigate("/", { replace: true });
      } else {
        navigate("/projects", { replace: true });
      }
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        toast.error("用户名或密码错误");
      } else if (error instanceof ApiError) {
        toast.error(`登录失败：${error.message}`);
      } else {
        toast.error("网络错误，请稍后重试");
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-muted/40 p-4">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle className="text-2xl">AgentOS</CardTitle>
          <CardDescription>Agent 协作工作流平台，请登录</CardDescription>
        </CardHeader>
        <CardContent>
          <Form {...form}>
            <form
              onSubmit={form.handleSubmit(onSubmit)}
              className="space-y-4"
              autoComplete="on"
            >
              <FormField
                control={form.control}
                name="username"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>用户名</FormLabel>
                    <FormControl>
                      <Input autoComplete="username" {...field} />
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
                    <FormLabel>密码</FormLabel>
                    <FormControl>
                      <Input
                        type="password"
                        autoComplete="current-password"
                        {...field}
                      />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <Button type="submit" className="w-full" disabled={submitting}>
                {submitting ? "登录中…" : "登录"}
              </Button>
            </form>
          </Form>
        </CardContent>
      </Card>
    </div>
  );
}
