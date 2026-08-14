import { useNavigate } from "react-router-dom";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { useAuthStore } from "../../app/store";
import { logout } from "../auth/session";

/**
 * 管理控制台（ticket 09 的分流落点；完整功能在 ticket 10 迭代）。
 * 仅全局管理员可访问（路由由 AdminOnly 守卫）。
 * 控制台是管理员登录后的唯一去处，暂为占位，至少提供登出入口避免卡死。
 */
export default function AdminConsolePage() {
  const navigate = useNavigate();
  const user = useAuthStore((s) => s.user);

  const handleLogout = async () => {
    await logout();
    toast.success("已登出");
    navigate("/login", { replace: true });
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-muted/40 p-4">
      <Card className="w-full max-w-lg">
        <CardHeader>
          <CardTitle>管理控制台</CardTitle>
          <CardDescription>平台级项目与账号管理</CardDescription>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">
            欢迎，{user?.username ?? "管理员"}。控制台功能将在后续版本提供。
          </p>
        </CardContent>
        <CardFooter>
          <Button
            variant="outline"
            className="w-full"
            onClick={() => void handleLogout()}
          >
            登出
          </Button>
        </CardFooter>
      </Card>
    </div>
  );
}
