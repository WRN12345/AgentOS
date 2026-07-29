import { NavLink, Outlet, useNavigate } from "react-router-dom";
import {
  Bot,
  ChevronsUpDown,
  ClipboardCheck,
  LayoutDashboard,
  ListTodo,
  LogOut,
  Package,
  Users,
} from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  SimpleMenu,
  simpleMenuItemClass,
} from "@/components/SimpleMenu";
import { Separator } from "@/components/ui/separator";
import { cn } from "@/lib/utils";
import { roleBadgeVariant, roleLabel } from "@/lib/roles";
import { api } from "../services/api";
import { useAuthStore } from "../app/store";
import { useEventStream } from "../services/events";
import { NotificationBell } from "../features/notifications/NotificationBell";

const navItems = [
  { to: "/", label: "团队看板", icon: LayoutDashboard, end: true },
  { to: "/members", label: "成员与能力", icon: Users, end: false },
  { to: "/work-items", label: "工作项", icon: ListTodo, end: false },
  { to: "/approvals", label: "审批中心", icon: ClipboardCheck, end: false },
  { to: "/deliverables", label: "交付物", icon: Package, end: false },
  { to: "/agent-assistant", label: "Agent 助手", icon: Bot, end: false },
];

/** 应用主布局：左侧导航 + 顶栏当前用户与登出入口。 */
export default function AppLayout() {
  const navigate = useNavigate();
  const { user, member, refreshToken, clear } = useAuthStore();

  // 全局 SSE：收到实时事件后失效对应查询缓存，页面无需手动刷新
  useEventStream();

  const handleLogout = async () => {
    // 登出即撤销 Refresh Token；失败也照常清空本地登录态
    try {
      if (refreshToken) {
        await api.post("/auth/logout", { refresh_token: refreshToken });
      }
    } catch {
      // 忽略登出接口错误
    }
    clear();
    toast.success("已登出");
    navigate("/login", { replace: true });
  };

  return (
    <div className="flex min-h-screen">
      {/* 侧边导航 */}
      <aside className="flex w-56 shrink-0 flex-col border-r bg-sidebar">
        <div className="flex h-14 items-center px-4">
          <span className="text-lg font-semibold">AgentOS</span>
        </div>
        <Separator />
        <nav className="flex-1 space-y-1 p-2">
          {navItems.map(({ to, label, icon: Icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              className={({ isActive }) =>
                cn(
                  "flex items-center gap-2 rounded-md px-3 py-2 text-sm font-medium transition-colors",
                  isActive
                    ? "bg-sidebar-accent text-sidebar-accent-foreground"
                    : "text-sidebar-foreground/70 hover:bg-sidebar-accent/60 hover:text-sidebar-accent-foreground",
                )
              }
            >
              <Icon className="size-4" />
              {label}
            </NavLink>
          ))}
        </nav>
      </aside>

      {/* 主区域 */}
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-14 items-center justify-between border-b px-6">
          <span className="text-sm text-muted-foreground">
            Agent 协作工作流平台
          </span>
          <div className="flex items-center gap-1">
            <NotificationBell />
            {/* 轻量下拉（不用 Radix）：部分环境下 Radix 触发器无响应，见 SimpleMenu 注释 */}
            <SimpleMenu
              contentClassName="w-48"
              trigger={(toggle, open) => (
                <Button variant="ghost" className="gap-2" onClick={toggle} aria-expanded={open}>
                  <span>{member?.display_name ?? user?.username ?? "用户"}</span>
                  {member && (
                    <Badge variant={roleBadgeVariant(member.role)}>
                      {roleLabel(member.role)}
                    </Badge>
                  )}
                  <ChevronsUpDown className="size-4 text-muted-foreground" />
                </Button>
              )}
            >
              {(close) => (
                <>
                  <div className="px-1.5 py-1.5 text-sm font-medium">
                    {user?.username ?? "当前用户"}
                  </div>
                  <div className="-mx-1 my-1 h-px bg-border" />
                  <button
                    type="button"
                    className={simpleMenuItemClass}
                    onClick={() => {
                      close();
                      void handleLogout();
                    }}
                  >
                    <LogOut className="size-4" />
                    登出
                  </button>
                </>
              )}
            </SimpleMenu>
          </div>
        </header>
        <main className="flex-1 p-6">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
