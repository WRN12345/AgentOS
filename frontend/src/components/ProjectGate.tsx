import { useEffect, type ReactNode } from "react";
import { Navigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Skeleton } from "@/components/ui/skeleton";
import { useAuthStore } from "../app/store";
import { isProjectRemembered } from "../features/auth/session";
import { api } from "../services/api";
import type { MyProject } from "../types";

/**
 * 工作台守卫：进入项目工作台前的分流（ticket 09）。
 * 管理员 → 管理控制台；普通用户未选项目、或上次选择已过 24h 记忆窗口 → 项目选择页；
 * 已选且仍在窗口内 → 放行工作台。
 */
export default function ProjectGate({ children }: { children: ReactNode }) {
  const { user, projects, currentProject, member, projectSelectedAt } = useAuthStore();
  const membershipCheck = useQuery({
    queryKey: ["my-projects", "workspace-gate", user?.id, currentProject?.id],
    queryFn: () => api.get<MyProject[]>("/auth/me/projects"),
    enabled: Boolean(user && !user.is_admin && currentProject),
    staleTime: 0,
    refetchOnMount: "always",
  });

  useEffect(() => {
    if (membershipCheck.data) {
      useAuthStore.getState().setProjects(membershipCheck.data);
    }
  }, [membershipCheck.data]);

  if (user?.is_admin) {
    return <Navigate to="/console" replace />;
  }
  if (currentProject && (membershipCheck.isPending || membershipCheck.isFetching)) {
    return <Skeleton className="h-24 w-full" aria-label="正在校验项目资格" />;
  }
  const latestProjects = membershipCheck.data ?? projects;
  const stillParticipating =
    currentProject != null && latestProjects?.some((project) => project.id === currentProject.id);
  if (
    !currentProject ||
    !member?.is_active ||
    membershipCheck.isError ||
    !stillParticipating ||
    !isProjectRemembered(projectSelectedAt)
  ) {
    return <Navigate to="/projects" replace />;
  }
  return <>{children}</>;
}
