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

/** GET /auth/me 返回的当前用户（含全局管理员标记；项目内角色从成员记录获取）。 */
export interface UserMe {
  id: string;
  username: string;
  is_active: boolean;
  is_admin: boolean;
  created_at: string;
}

/** 项目成员角色：负责人全管，成员普通参与；管理员为全局角色，不再属于项目。 */
export type MemberRole = "leader" | "member";

/** GET /auth/me/projects 返回的用户参与项目摘要。 */
export interface MyProject {
  id: string;
  name: string;
  description: string | null;
  role: MemberRole;
}

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

/**
 * 成员添加结果（POST /members 复用已有账号场景，无初始密码，恒为 null）。
 * 建号收敛到 admin 后，一次性初始密码只在 admin 控制台新建账号时返回。
 */
export interface MemberWithPassword extends Member {
  initial_password: string | null;
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

/* ---------- 记忆模块：核心记忆（设计文档第 8 节，M4.3） ---------- */

/** 核心记忆条目；proposed_by 为 null 表示 Agent 提议（负责人确认后生效）。 */
export interface CoreMemoryEntry {
  id: string;
  scope: string;
  content: string;
  status: "active" | "deprecated";
  proposed_by: MemberBrief | null;
  confirmed_by: MemberBrief;
  effective_at: string;
  created_at: string;
}

/** GET /memory/core-entries 响应：条目列表 + 容量占用。 */
export interface CoreMemoryEntryList {
  entries: CoreMemoryEntry[];
  used_chars: number;
  budget_chars: number;
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
  kind: "transfer" | "deadline_change" | "dev_doc" | "delivery_review";
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
  /** 已处理记录（GET /approvals/processed）额外返回：处理人与处理时间；发起人撤销时为 null。 */
  approved_by?: MemberBrief | null;
  approved_at?: string | null;
  /** kind="dev_doc" 时返回：第 N 次提交与打回理由。 */
  doc_version?: number;
  review_note?: string | null;
  /** kind="delivery_review" 时返回：被审核的交付物版本与类型；status 为审核结论。 */
  deliverable_version?: number | null;
  deliverable_type?: string | null;
}

/** 交付物列表项（GET /deliverables 聚合页；?role=mine 为"我的申请"我的交付）。 */
export interface DeliverableListItem {
  id: string;
  work_item_id: string;
  work_item_title: string;
  type: DeliverableType;
  version: number;
  submitted_by: MemberBrief;
  created_at: string;
  review: {
    decision: ReviewDecision;
    feedback: string | null;
    reviewed_by: MemberBrief;
    created_at: string;
  } | null;
}

/* ---------- 开发文档前置（2026-07-30 设计文档 §4） ---------- */

export type DevDocStatus = "DRAFT" | "SUBMITTED" | "CONFIRMED" | "RETURNED";

/** 开发文档（GET /work-items/{id}/dev-doc；404 表示还没有文档）。 */
export interface DevDoc {
  id: string;
  work_item_id: string;
  work_item_title: string;
  author: MemberBrief | null;
  content: string;
  status: DevDocStatus;
  /** 负责人打回理由（RETURNED 时有值）。 */
  review_note: string | null;
  confirmed_by: MemberBrief | null;
  confirmed_at: string | null;
  /** 每次提交 +1（"第 N 次提交"）。 */
  doc_version: number;
  /** 负责人已豁免该任务的文档要求。 */
  waived: boolean;
  /** 最新一条 AI 初审建议（dev_doc_review）id，LLM 降级时为 null。 */
  latest_review_suggestion_id: string | null;
  version: number;
  created_at: string;
  updated_at: string;
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
  /** 版本链（设计文档第 3 节）：同名上传递增；superseded_by 非空表示已被新版本取代。 */
  version: number;
  superseded_by: string | null;
  /** 索引状态（设计文档第 6 节）。 */
  index_status: IndexStatus;
  created_at: string;
  updated_at: string;
}

/** 索引状态机（设计文档第 6 节）。 */
export type IndexStatus = "pending" | "indexing" | "indexed" | "failed" | "unindexed";

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

/* ---------- 阶段 5：多 Agent 辅助 ---------- */

export type AgentSuggestionReviewStatus = "pending" | "accepted" | "ignored";

/**
 * Agent 建议（GET /agent-suggestions，12.5 节，T5.7）。
 * content 统一含 summary/rationale，其余字段随 suggestion_type 扩展
 * （见 features/agent-assistant/SuggestionContent.tsx 的结构化渲染）。
 */
export interface AgentSuggestion {
  id: string;
  run_id: string;
  suggestion_type: string;
  content: Record<string, unknown> & { summary?: string; rationale?: string };
  confidence: number | null;
  risks: string | null;
  fact_refs: Record<string, string[]> | null;
  review_status: AgentSuggestionReviewStatus;
  reviewed_by: string | null;
  reviewed_at: string | null;
  prompt_version: string | null;
  /** 关联工作项（项目级建议为 null，不渲染链接）。 */
  work_item_id: string | null;
  model: string | null;
  created_at: string;
}

/* ---------- 需求拆解流水线（2026-07-30 设计文档 §4.2） ---------- */

/** requirement_pipeline 建议中的成员推荐（真实 member_id + 推荐理由）。 */
export interface PipelineAssigneeCandidate {
  member_id: string;
  display_name: string;
  reason?: string;
}

/** requirement_pipeline 建议 content.work_item_breakdown 的单项。 */
export interface PipelineBreakdownItem {
  title: string;
  description?: string;
  acceptance_criteria?: string;
  /** Agent 输出 P0–P3，前端创建时映射为 WorkItemPriority。 */
  priority?: string;
  suggested_due_at?: string | null;
  recommended_assignee?: PipelineAssigneeCandidate | null;
  candidates?: PipelineAssigneeCandidate[];
  /** 需求文本中点名指定的人选（Agent 不更改，仅做合理性校验）。 */
  user_specified?: boolean;
}

/** requirement_pipeline 类型建议的 content 载荷。 */
export interface RequirementPipelineContent {
  summary?: string;
  rationale?: string;
  goals?: string[];
  constraints?: string[];
  deliverables?: string[];
  acceptance_criteria?: string[];
  /** 需求涉及的方面/技术点（取自成员技能标签词表）。 */
  involved_aspects?: string[];
  work_item_breakdown?: PipelineBreakdownItem[];
  collaboration_points?: string[];
  /** 需求文本中点名但匹配不到成员的名字。 */
  unresolved_mentions?: string[];
  risks?: string[];
}

/** Agent 运行记录（GET /agent-runs[/{id}]，T5.7 列表/详情带错误与耗时）。 */
export interface AgentRun {
  id: string;
  agent_type: string;
  status: "pending" | "running" | "succeeded" | "failed";
  model: string | null;
  trigger_source: string;
  work_item_id: string | null;
  request_id: string | null;
  created_at: string;
  error: string | null;
  duration_ms: number | null;
  retry_count: number;
}

/** GET /config 返回的前端可用配置（16 节：外部数据提示）。 */
export interface AgentConfig {
  llm_provider: string;
  llm_is_external: boolean;
}

/* ---------- 管理控制台（ticket 10，仅全局管理员） ---------- */

/** 项目负责人摘要（GET /projects 内嵌；管理员视角，非成员记录）。 */
export interface LeaderBrief {
  id: string;
  user_id: string;
  username: string;
  display_name: string;
}

/** GET /projects 返回的项目摘要（全局管理员视角，含负责人）。 */
export interface AdminProject {
  id: string;
  name: string;
  description: string | null;
  /** 创建项目即指定的唯一负责人；历史数据可能为 null。 */
  leader: LeaderBrief | null;
  created_at: string;
  updated_at: string;
}

/** POST /admin/users 响应：全局账号 + 一次性初始密码（仅此一次返回，之后不可再查）。 */
export interface CreatedAccount extends UserMe {
  initial_password: string;
}
