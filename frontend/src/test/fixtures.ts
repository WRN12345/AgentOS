import type {
  Deliverable,
  Member,
  MemberBrief,
  MyProject,
  TokenPair,
  UserMe,
  WorkItem,
  WorkItemSummary,
} from "../types";

/** 测试夹具：构造各接口返回形状的最小合法数据。 */

export const tokens: TokenPair = {
  access_token: "test-access-token",
  refresh_token: "test-refresh-token",
  token_type: "bearer",
  expires_in: 1800,
};

export function makeUser(overrides: Partial<UserMe> = {}): UserMe {
  return {
    id: "user-1",
    username: "alice",
    is_active: true,
    is_admin: false,
    created_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

/** 用户参与的项目（GET /auth/me/projects 形状）。 */
export function makeProject(overrides: Partial<MyProject> = {}): MyProject {
  return {
    id: "project-1",
    name: "Alpha",
    description: null,
    role: "member",
    ...overrides,
  };
}

export function makeMember(overrides: Partial<Member> = {}): Member {
  return {
    id: "member-1",
    user_id: "user-1",
    username: "alice",
    role: "member",
    display_name: "爱丽丝",
    weekly_available_hours: 20,
    git_username: null,
    is_active: true,
    active_work_items: 0,
    capabilities: [],
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

/** 负责人成员（角色 leader）。 */
export function makeLeader(overrides: Partial<Member> = {}): Member {
  return makeMember({
    id: "member-leader",
    user_id: "user-leader",
    username: "leader",
    role: "leader",
    display_name: "负责人",
    ...overrides,
  });
}

export function memberBrief(
  overrides: Partial<MemberBrief> = {},
): MemberBrief {
  return { id: "member-1", display_name: "爱丽丝", ...overrides };
}

export function makeWorkItemSummary(
  overrides: Partial<WorkItemSummary> = {},
): WorkItemSummary {
  return {
    id: "wi-1",
    title: "RAG 检索管道",
    status: "IN_PROGRESS",
    priority: "high",
    assignee: memberBrief(),
    due_at: null,
    version: 1,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

export function makeWorkItem(overrides: Partial<WorkItem> = {}): WorkItem {
  return {
    ...makeWorkItemSummary(overrides),
    description: null,
    acceptance_criteria: null,
    collaborators: [],
    ...overrides,
  };
}

export function makeDeliverable(
  overrides: Partial<Deliverable> = {},
): Deliverable {
  return {
    id: "del-1",
    work_item_id: "wi-1",
    type: "git_link",
    content: "https://github.com/org/repo/pull/1",
    file: null,
    version: 1,
    submitted_by: memberBrief(),
    created_at: "2026-01-02T00:00:00Z",
    updated_at: "2026-01-02T00:00:00Z",
    ...overrides,
  };
}
