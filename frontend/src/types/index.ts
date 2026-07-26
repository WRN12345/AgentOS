/** API 统一错误格式（设计文档 17.1 节）。 */
export interface ApiErrorBody {
  code: string;
  message: string;
  request_id: string;
  details?: Record<string, unknown>;
}

export interface HealthResponse {
  status: string;
  checks: Record<string, string>;
}

/** 登录/刷新接口返回的令牌对（12.1 节）。 */
export interface TokenPair {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_in: number;
}

/** GET /auth/me 返回的当前用户（不含角色，角色从成员记录获取）。 */
export interface UserMe {
  id: string;
  username: string;
  is_active: boolean;
  created_at: string;
}

export type MemberRole = "leader" | "member";

/** 成员能力标签（6.2 节）：熟练度 1-5，需负责人确认。 */
export interface MemberCapability {
  id: string;
  tag: string;
  proficiency: number;
  confirmed: boolean;
  confirmed_by_member_id: string | null;
  confirmed_at: string | null;
}

/** GET /members 返回的成员摘要（含工作量统计，不含隐私字段）。 */
export interface Member {
  id: string;
  user_id: string;
  username: string;
  role: MemberRole;
  display_name: string;
  weekly_available_hours: number | null;
  git_username: string | null;
  is_active: boolean;
  active_work_items: number;
  capabilities: MemberCapability[];
  created_at: string;
  updated_at: string;
}

/** POST /members 响应：额外携带仅返回一次的初始密码。 */
export interface MemberWithPassword extends Member {
  initial_password: string;
}

export type WorkItemPriority = "low" | "medium" | "high" | "urgent";

export type WorkItemStatus =
  | "DRAFT"
  | "READY"
  | "IN_PROGRESS"
  | "BLOCKED"
  | "IN_REVIEW"
  | "COMPLETED"
  | "CANCELLED";

/** 工作项成员摘要（主执行人/协作者）。 */
export interface WorkItemMemberRef {
  id: string;
  display_name: string;
}

/** GET /work-items 列表摘要（无说明、验收标准与协作者）。 */
export interface WorkItemSummary {
  id: string;
  title: string;
  status: WorkItemStatus;
  priority: WorkItemPriority;
  assignee: WorkItemMemberRef;
  due_at: string | null;
  version: number;
  created_at: string;
  updated_at: string;
}

/** GET /work-items/{id} 返回的工作项完整字段。 */
export interface WorkItem extends WorkItemSummary {
  description: string | null;
  acceptance_criteria: string | null;
  collaborators: WorkItemMemberRef[];
}
