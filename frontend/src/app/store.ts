import { create } from "zustand";

interface AuthState {
  token: string | null;
  username: string | null;
  setAuth: (token: string, username: string) => void;
  clear: () => void;
}

/** 轻量全局状态（阶段 2 接入真实登录后填充 token）。 */
export const useAuthStore = create<AuthState>((set) => ({
  token: null,
  username: null,
  setAuth: (token, username) => set({ token, username }),
  clear: () => set({ token: null, username: null }),
}));
