import { useAuthStore } from "../../app/store";
import { api } from "../../services/api";
import type { Member, MyProject, UserMe } from "../../types";

/**
 * 加载当前用户参与的项目列表（GET /auth/me/projects，免项目头）。
 * 返回列表供调用方决定是否自动选中（ticket 09 用项目选择器替代）。
 */
export async function loadProjects(): Promise<MyProject[]> {
  // 先清掉上次登录持久化的 currentProject（含其 member）：
  // 重新登录即重建立项目上下文，避免残留旧项目头打到错误项目；
  // 选定项目由调用方（selectProject / ticket 09 选择器）重新设置。
  useAuthStore.getState().setCurrentProject(null);
  const projects = await api.get<MyProject[]>("/auth/me/projects");
  useAuthStore.getState().setProjects(projects);
  return projects;
}

/**
 * 选定当前项目并加载该项目下的成员身份。
 * setCurrentProject 会清空上一项目的 member，随后 loadIdentity 重新加载本项目成员。
 */
export async function selectProject(project: MyProject): Promise<void> {
  useAuthStore.getState().setCurrentProject(project);
  await loadIdentity();
}

/**
 * 加载当前登录身份：GET /auth/me 拿用户；
 * 仅当已选定项目时再 GET /members 匹配该项目下的成员记录。
 * 未选项目（全局管理员 / 多个项目待分流）时不打 /members——
 * 后端对该接口要求 X-Project-Id，缺 header 会报 400。
 */
export async function loadIdentity(): Promise<void> {
  const { setIdentity } = useAuthStore.getState();
  const user = await api.get<UserMe>("/auth/me");
  const projectId = useAuthStore.getState().currentProject?.id;
  let member: Member | null = null;
  if (projectId) {
    const members = await api.get<Member[]>("/members");
    member = members.find((m) => m.user_id === user.id) ?? null;
  }
  setIdentity(user, member);
}
