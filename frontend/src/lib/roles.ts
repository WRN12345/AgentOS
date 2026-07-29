import type { MemberRole } from "../types";

/** 角色徽标文案：负责人 / 管理员 / 成员。 */
export function roleLabel(role: MemberRole): string {
  switch (role) {
    case "leader":
      return "负责人";
    case "admin":
      return "管理员";
    default:
      return "成员";
  }
}

/** 角色徽标样式：负责人主色，管理员描边，成员次色。 */
export function roleBadgeVariant(
  role: MemberRole,
): "default" | "outline" | "secondary" {
  switch (role) {
    case "leader":
      return "default";
    case "admin":
      return "outline";
    default:
      return "secondary";
  }
}
