import type { ReactNode } from "react";
import { Navigate } from "react-router-dom";
import { useAuthStore } from "../app/store";
import { isProjectRemembered } from "../features/auth/session";

/**
 * 工作台守卫：进入项目工作台前的分流（ticket 09）。
 * 管理员 → 管理控制台；普通用户未选项目、或上次选择已过 24h 记忆窗口 → 项目选择页；
 * 已选且仍在窗口内 → 放行工作台。
 */
export default function ProjectGate({ children }: { children: ReactNode }) {
  const { user, currentProject, projectSelectedAt } = useAuthStore();

  if (user?.is_admin) {
    return <Navigate to="/console" replace />;
  }
  if (!currentProject || !isProjectRemembered(projectSelectedAt)) {
    return <Navigate to="/projects" replace />;
  }
  return <>{children}</>;
}
