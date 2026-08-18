import { useAuthStore } from "../../app/store";
import { queryClient } from "../../app/queryClient";
import { api } from "../../services/api";
import type { Member, MyProject, UserMe } from "../../types";

/** 上次项目可"直接进入"的记忆窗口：24 小时（本地存储未过期即直接进入）。 */
export const PROJECT_REMEMBER_MS = 24 * 60 * 60 * 1000;

/** 上次项目选择时间戳是否仍在 24h 记忆窗口内（null=从未选过/已清空）。 */
export function isProjectRemembered(selectedAt: number | null): boolean {
  return selectedAt != null && Date.now() - selectedAt < PROJECT_REMEMBER_MS;
}

/**
 * 从登录前的持久化上下文挑选可自动进入的项目：
 * 仅当上次项目仍在 24h 记忆窗口内、且仍在我参与的项目列表中才采用，
 * 否则返回 null（进选择页重新分流）。
 */
export function pickRememberedProject(
  projects: MyProject[],
  rememberedProject: MyProject | null,
  rememberedAt: number | null,
): MyProject | null {
  if (!rememberedProject || !isProjectRemembered(rememberedAt)) {
    return null;
  }
  return projects.find((p) => p.id === rememberedProject.id) ?? null;
}

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
  const previous = useAuthStore.getState();
  useAuthStore.getState().setCurrentProject(project);
  try {
    await loadIdentity();
  } catch (error) {
    // 项目上下文与成员身份必须原子切换；加载失败时完整恢复上一份可用上下文。
    useAuthStore.setState({
      currentProject: previous.currentProject,
      member: previous.member,
      projectSelectedAt: previous.projectSelectedAt,
      user: previous.user,
    });
    throw error;
  }
}

/** 登出：尽力撤销 Refresh Token 后清空本地登录态（接口失败也照常清空）。 */
export async function logout(): Promise<void> {
  const { refreshToken, clear } = useAuthStore.getState();
  try {
    if (refreshToken) {
      await api.post("/auth/logout", { refresh_token: refreshToken });
    }
  } catch {
    // 忽略登出接口错误
  }
  queryClient.clear();
  clear();
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
