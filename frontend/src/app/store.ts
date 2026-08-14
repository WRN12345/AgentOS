import { create } from "zustand";
import { persist } from "zustand/middleware";
import type { Member, MyProject, TokenPair, UserMe } from "../types";

interface AuthState {
  accessToken: string | null;
  refreshToken: string | null;
  /** 当前登录用户（GET /auth/me，含全局管理员标记 is_admin）。 */
  user: UserMe | null;
  /** 当前用户参与的项目列表（GET /auth/me/projects）。 */
  projects: MyProject[] | null;
  /** 当前选中的项目；member 是该项目下的成员记录。 */
  currentProject: MyProject | null;
  /** 当前用户在 currentProject 下的成员记录（角色、能力等）。 */
  member: Member | null;
  setTokens: (tokens: TokenPair) => void;
  setIdentity: (user: UserMe, member: Member | null) => void;
  setMember: (member: Member | null) => void;
  setProjects: (projects: MyProject[]) => void;
  /** 切换当前项目；同时置空 member，避免残留上一项目的成员记录。 */
  setCurrentProject: (project: MyProject | null) => void;
  clear: () => void;
}

/** 登录态全局存储：令牌与身份信息持久化到 localStorage，刷新页面不丢。 */
export const useAuthStore = create<AuthState>()(
  persist(
    (set) => ({
      accessToken: null,
      refreshToken: null,
      user: null,
      projects: null,
      currentProject: null,
      member: null,
      setTokens: (tokens) =>
        set({
          accessToken: tokens.access_token,
          refreshToken: tokens.refresh_token,
        }),
      setIdentity: (user, member) => set({ user, member }),
      setMember: (member) => set({ member }),
      setProjects: (projects) => set({ projects }),
      setCurrentProject: (project) =>
        set({ currentProject: project, member: null }),
      clear: () =>
        set({
          accessToken: null,
          refreshToken: null,
          user: null,
          projects: null,
          currentProject: null,
          member: null,
        }),
    }),
    { name: "agentos-auth" },
  ),
);

/** 便捷选择器：当前用户是否为项目负责人。 */
export const useIsLeader = () =>
  useAuthStore((s) => s.member?.role === "leader");

/** 便捷选择器：当前用户是否为全局管理员（users.is_admin，不参与业务协作）。 */
export const useIsAdmin = () => useAuthStore((s) => s.user?.is_admin === true);

/** 便捷选择器：当前用户是否可管理成员账号（项目负责人或全局管理员同权）。 */
export const useCanManageMembers = () =>
  useAuthStore(
    (s) => s.member?.role === "leader" || s.user?.is_admin === true,
  );
