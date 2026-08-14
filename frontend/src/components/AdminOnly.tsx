import type { ReactNode } from "react";
import { Navigate } from "react-router-dom";
import { useAuthStore } from "../app/store";

/** 管理控制台守卫：仅全局管理员可访问；普通用户回到工作台（无项目时由工作台守卫再分流）。 */
export default function AdminOnly({ children }: { children: ReactNode }) {
  const isAdmin = useAuthStore((s) => s.user?.is_admin === true);

  if (!isAdmin) {
    return <Navigate to="/" replace />;
  }
  return <>{children}</>;
}
