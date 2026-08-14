import { useEffect, useState } from "react";
import { Navigate, useNavigate } from "react-router-dom";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { roleBadgeVariant, roleLabel } from "@/lib/roles";
import { useAuthStore } from "../../app/store";
import { loadProjects, logout, selectProject } from "./session";
import type { MyProject } from "../../types";

/**
 * 项目选择页（ticket 09）：登录分流后普通用户在此选择进入哪个项目。
 * 列出我参与的项目（带角色徽章），点选即选定并进入工作台。
 * 每次进入都重拉最新列表，避免持久化的旧列表误导选择。
 */
export default function ProjectPickerPage() {
  const navigate = useNavigate();
  const user = useAuthStore((s) => s.user);
  const projects = useAuthStore((s) => s.projects) ?? [];
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  // 进入即刷新：loadProjects 同时清空上次的 currentProject，重新建立项目上下文
  useEffect(() => {
    let cancelled = false;
    loadProjects()
      .catch(() => {
        if (!cancelled) setError(true);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (user?.is_admin) {
    return <Navigate to="/console" replace />;
  }

  const handleSelect = async (project: MyProject) => {
    try {
      await selectProject(project);
      navigate("/", { replace: true });
    } catch {
      toast.error("进入项目失败，请重试");
    }
  };

  const reload = () => {
    setLoading(true);
    setError(false);
    void loadProjects()
      .catch(() => setError(true))
      .finally(() => setLoading(false));
  };

  const handleLogout = async () => {
    await logout();
    navigate("/login", { replace: true });
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-muted/40 p-4">
      <Card className="w-full max-w-lg">
        <CardHeader>
          <CardTitle>选择项目</CardTitle>
          <CardDescription>
            选择要进入的项目，进入后全站数据按该项目隔离
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {loading ? (
            <div className="space-y-2">
              <Skeleton className="h-14 w-full" />
              <Skeleton className="h-14 w-full" />
            </div>
          ) : error ? (
            <div className="space-y-2">
              <p className="text-sm text-destructive">
                加载项目列表失败，请重试
              </p>
              <Button variant="outline" onClick={reload}>
                重试
              </Button>
            </div>
          ) : projects.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              你还没有参与任何项目
            </p>
          ) : (
            <ul className="space-y-2">
              {projects.map((project) => (
                <li key={project.id}>
                  <button
                    type="button"
                    onClick={() => void handleSelect(project)}
                    className="flex w-full items-center justify-between gap-3 rounded-md border p-3 text-left transition-colors hover:bg-accent"
                  >
                    <span className="min-w-0">
                      <span className="block font-medium">{project.name}</span>
                      {project.description && (
                        <span className="block truncate text-sm text-muted-foreground">
                          {project.description}
                        </span>
                      )}
                    </span>
                    <Badge variant={roleBadgeVariant(project.role)}>
                      {roleLabel(project.role)}
                    </Badge>
                  </button>
                </li>
              ))}
            </ul>
          )}
          {/* 选择页是登录后的独立页，提供登出口避免卡死 */}
          <Button
            variant="outline"
            className="w-full"
            onClick={() => void handleLogout()}
          >
            登出
          </Button>
        </CardContent>
      </Card>
    </div>
  );
}
