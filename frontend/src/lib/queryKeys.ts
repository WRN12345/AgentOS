import { useAuthStore } from "../app/store";

/**
 * React Query 缓存键工厂：业务数据按当前项目隔离。
 *
 * 多项目化后，同一接口（/work-items、/members…）在不同项目下是不同数据集，
 * 因此所有项目维度缓存的键统一以当前项目 id 打头：[projectId, root, ...suffix]。
 * 切换项目即得到全新缓存条目，避免 A 项目数据串进 B 项目；失效也以该前缀为准，
 * 只刷新当前项目的数据。
 *
 * 全局数据（config、audit-events）不随项目变化，不经本工厂。
 * 未选定项目时（登录分流前 / 全局管理员）不加前缀，键为 [root, ...suffix]，
 * 与选定项目后的键天然隔离，管理员视角不会误读某项目缓存。
 */
function scoped(root: string, ...suffix: unknown[]): unknown[] {
  const projectId = useAuthStore.getState().currentProject?.id;
  return projectId ? [projectId, root, ...suffix] : [root, ...suffix];
}

/** 项目维度缓存根：全部业务查询键与失效前缀统一经此生成。 */
export const queryKeys = {
  workItems: (...suffix: unknown[]) => scoped("work-items", ...suffix),
  members: (...suffix: unknown[]) => scoped("members", ...suffix),
  approvals: (...suffix: unknown[]) => scoped("approvals", ...suffix),
  collaborationRequests: (...suffix: unknown[]) =>
    scoped("collaboration-requests", ...suffix),
  transferRequests: (...suffix: unknown[]) =>
    scoped("transfer-requests", ...suffix),
  deadlineChangeRequests: (...suffix: unknown[]) =>
    scoped("deadline-change-requests", ...suffix),
  deliverables: (...suffix: unknown[]) => scoped("deliverables", ...suffix),
  reviews: (...suffix: unknown[]) => scoped("reviews", ...suffix),
  devDoc: (...suffix: unknown[]) => scoped("dev-doc", ...suffix),
  agentSuggestions: (...suffix: unknown[]) =>
    scoped("agent-suggestions", ...suffix),
  agentRuns: (...suffix: unknown[]) => scoped("agent-runs", ...suffix),
  notifications: (...suffix: unknown[]) => scoped("notifications", ...suffix),
};
