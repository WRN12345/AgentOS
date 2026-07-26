import { useAuthStore } from "../../app/store";
import { api } from "../../services/api";
import type { Member, UserMe } from "../../types";

/**
 * 加载当前登录身份：GET /auth/me 拿用户，再在 GET /members 中按 user_id
 * 匹配自己的成员记录（角色、能力），写入全局 store。
 */
export async function loadIdentity(): Promise<void> {
  const { setIdentity } = useAuthStore.getState();
  const user = await api.get<UserMe>("/auth/me");
  const members = await api.get<Member[]>("/members");
  const member = members.find((m) => m.user_id === user.id) ?? null;
  setIdentity(user, member);
}
