import type { MemberRole } from "../types";

/** 角色徽标文案：负责人 / 成员。 */
export function roleLabel(role: MemberRole): string {
  switch (role) {
    case "leader":
      return "负责人";
    default:
      return "成员";
  }
}

/** 角色徽标样式：负责人主色，成员次色。 */
export function roleBadgeVariant(
  role: MemberRole,
): "default" | "secondary" {
  switch (role) {
    case "leader":
      return "default";
    default:
      return "secondary";
  }
}
