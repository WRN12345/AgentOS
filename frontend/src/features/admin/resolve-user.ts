import type { UserMe } from "../../types";

export type UserResolveHint =
  | { tone: "ok"; text: string }
  | { tone: "err"; text: string }
  | null;

export interface UserResolveResult {
  /** 去除首尾空白的输入。 */
  typed: string;
  /** 按用户名命中的账号（含 admin/已禁用，仅用于给出更精确的提示）。 */
  match: UserMe | null;
  /** 可担任负责人的账号（非 admin、已启用）；未命中为 null。 */
  resolved: UserMe | null;
  hint: UserResolveHint;
}

/**
 * 按完整用户名解析可担任负责人的账号（无搜索端点，输入完整用户名）。
 * 建号收敛到 admin 后，负责人只能是普通启用用户（全局管理员不参与项目业务，16 节）。
 * 项目创建、变更负责人两处共用。
 */
export function resolveUser(
  users: UserMe[] | undefined,
  username: string,
): UserResolveResult {
  const typed = username?.trim() ?? "";
  const match = (users ?? []).find((u) => u.username === typed) ?? null;
  const resolved =
    match && !match.is_admin && match.is_active ? match : null;
  const hint: UserResolveHint =
    typed === ""
      ? null
      : resolved
        ? { tone: "ok", text: "✓ 账号有效，可担任负责人" }
        : match?.is_admin
          ? { tone: "err", text: "全局管理员不能担任项目负责人" }
          : match && !match.is_active
            ? { tone: "err", text: "该账号已禁用" }
            : { tone: "err", text: "未找到该账号" };
  return { typed, match, resolved, hint };
}
