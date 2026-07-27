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

/* ---------- 阶段 3：动态协作 ---------- */

/** 成员摘要（协作/转派/DDL 接口的 MemberBrief）。 */
export interface MemberBrief {
  id: string;
  display_name: string;
}

/** 协作请求状态（8.2 节）。 */
export type CollaborationStatus =
  | "REQUESTED"
  | "ACCEPTED"
  | "DECLINED"
  | "IN_PROGRESS"
  | "SUBMITTED"
  | "REVISION_REQUESTED"
  | "COMPLETED"
  | "CANCELLED";

/** 协作请求摘要（列表接口返回；不含 goal/template/result_text 正文）。 */
export interface CollaborationRequestSummary {
  id: string;
  work_item_id: string;
  work_item_title: string;
  requester: MemberBrief;
  assignee: MemberBrief;
  title: string;
  status: CollaborationStatus;
  due_at: string | null;
  version: number;
  created_at: string;
  updated_at: string;
}

/** 协作请求完整字段（命令接口响应）。 */
export interface CollaborationRequest extends CollaborationRequestSummary {
  goal: string;
  template: string | null;
  result_text: string | null;
}

/** 转派申请状态（8.3 节）。 */
export type TransferStatus = "PENDING" | "APPROVED" | "REJECTED" | "CANCELLED";

/** 转派申请摘要（列表接口返回；不含 reason/impact_note 正文）。 */
export interface TransferRequestSummary {
  id: string;
  work_item_id: string;
  work_item_title: string;
  from_member: MemberBrief;
  to_member: MemberBrief;
  status: TransferStatus;
  version: number;
  created_at: string;
  updated_at: string;
}

/** 转派申请完整字段（详情接口返回）。 */
export interface TransferRequest extends TransferRequestSummary {
  reason: string;
  impact_note: string;
  agent_suggestion_id: string | null;
  approved_by: MemberBrief | null;
  approved_at: string | null;
}

/** DDL 变更申请状态（8.4 节）。 */
export type DeadlineChangeStatus =
  | "PENDING_IMPACT_ANALYSIS"
  | "PENDING_APPROVAL"
  | "APPROVED"
  | "REJECTED"
  | "CANCELLED";

export type DeadlineTargetType = "work_item" | "collaboration_request";

/** DDL 变更申请摘要（列表接口返回；不含 reason 与 impact_analysis 正文）。 */
export interface DeadlineChangeSummary {
  id: string;
  work_item_id: string;
  work_item_title: string;
  target_type: DeadlineTargetType;
  target_id: string;
  target_title: string;
  old_due_at: string | null;
  new_due_at: string;
  impact_analysis_status: "generated" | "unavailable";
  status: DeadlineChangeStatus;
  requested_by: MemberBrief;
  version: number;
  created_at: string;
  updated_at: string;
}

/** 规则化影响分析内容（7.4、8.4 节，DDL 变更详情接口返回）。 */
export interface DeadlineImpactAnalysis {
  target: {
    type: DeadlineTargetType;
    id: string;
    old_due_at: string | null;
    new_due_at: string | null;
  };
  work_item: { id: string; title: string; due_at: string | null };
  exceeds_work_item_due: boolean;
  affected_collaboration_requests: {
    id: string;
    title: string;
    status: string;
    requester_id: string;
    assignee_id: string;
    due_at: string | null;
  }[];
}

/** DDL 变更申请完整字段（详情接口返回）。 */
export interface DeadlineChangeRequest extends DeadlineChangeSummary {
  reason: string;
  impact_analysis: DeadlineImpactAnalysis | null;
  approved_by: MemberBrief | null;
  approved_at: string | null;
}

/** 负责人待审批聚合项（GET /approvals）：转派与 DDL 变更统一形状。 */
export interface ApprovalItem {
  kind: "transfer" | "deadline_change";
  id: string;
  work_item_id: string;
  work_item_title: string;
  summary: string;
  requested_by: MemberBrief;
  status: string;
  impact_analysis_status: "generated" | "unavailable" | null;
  version: number;
  created_at: string;
  updated_at: string;
  from_member: MemberBrief | null;
  to_member: MemberBrief | null;
  target_type: DeadlineTargetType | null;
  target_id: string | null;
  old_due_at: string | null;
  new_due_at: string | null;
}

/* ---------- 阶段 4：交付与审核 ---------- */

/** POST /files 响应：服务端落库的文件记录（不含 storage_key，16 节最小暴露）。 */
export interface StoredFile {
  id: string;
  original_filename: string;
  size_bytes: number;
  mime_type: string;
  sha256: string;
  storage_backend: string;
  uploaded_by: string;
  work_item_id: string | null;
  created_at: string;
  updated_at: string;
}

export type DeliverableType = "git_link" | "text" | "file";

/** file 类型交付物内嵌的文件摘要（含 sha256 供完整性追溯）。 */
export interface FileBrief {
  id: string;
  original_filename: string;
  size_bytes: number;
  mime_type: string;
  sha256: string;
}

/** 交付物版本（7.5 节：每次提交生成新版本，旧版本保留可查）。 */
export interface Deliverable {
  id: string;
  work_item_id: string;
  type: DeliverableType;
  content: string | null;
  file: FileBrief | null;
  version: number;
  submitted_by: MemberBrief;
  created_at: string;
  updated_at: string;
}

export type ReviewDecision = "approve" | "request_changes" | "reject";

/** 最终审核记录（7.5 节）：反馈正文仅负责人与主执行人可见（16 节）。 */
export interface Review {
  id: string;
  work_item_id: string;
  deliverable_id: string;
  deliverable_version: number;
  decision: ReviewDecision;
  feedback: string | null;
  reviewed_by: MemberBrief;
  /** 审核生效后的工作项状态。 */
  work_item_status: WorkItemStatus;
  created_at: string;
  updated_at: string;
}

/** 站内通知（12.6 节）。 */
export interface AppNotification {
  id: string;
  type: string;
  title: string;
  body: string;
  link: string | null;
  is_read: boolean;
  read_at: string | null;
  created_at: string;
}

export interface NotificationList {
  items: AppNotification[];
  unread_count: number;
}

/** 审计事件（GET /audit-events，仅负责人）。 */
export interface AuditEvent {
  id: string;
  actor_id: string | null;
  action: string;
  target_type: string | null;
  target_id: string | null;
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
  request_id: string | null;
  source_ip: string | null;
  created_at: string;
}

/** SSE 事件载荷（/events/stream 帧 data 段）。 */
export interface RealtimeEvent {
  id: string;
  type: string;
  data: { title: string; body: string; link: string | null };
  created_at: string;
}
