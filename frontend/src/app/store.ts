import { create } from "zustand";
import { persist } from "zustand/middleware";
import type { Member, TokenPair, UserMe } from "../types";

interface AuthState {
  accessToken: string | null;
  refreshToken: string | null;
  /** 当前登录用户（GET /auth/me）。 */
  user: UserMe | null;
  /** 当前用户的项目成员记录（角色、能力等，从 GET /members 匹配 user_id 得到）。 */
  member: Member | null;
  setTokens: (tokens: TokenPair) => void;
  setIdentity: (user: UserMe, member: Member | null) => void;
  setMember: (member: Member | null) => void;
  clear: () => void;
}

/** 登录态全局存储：令牌与身份信息持久化到 localStorage，刷新页面不丢。 */
export const useAuthStore = create<AuthState>()(
  persist(
    (set) => ({
      accessToken: null,
      refreshToken: null,
      user: null,
      member: null,
      setTokens: (tokens) =>
        set({
          accessToken: tokens.access_token,
          refreshToken: tokens.refresh_token,
        }),
      setIdentity: (user, member) => set({ user, member }),
      setMember: (member) => set({ member }),
      clear: () =>
        set({
          accessToken: null,
          refreshToken: null,
          user: null,
          member: null,
        }),
    }),
    { name: "agentos-auth" },
  ),
);

/** 便捷选择器：当前用户是否为项目负责人。 */
export const useIsLeader = () =>
  useAuthStore((s) => s.member?.role === "leader");
