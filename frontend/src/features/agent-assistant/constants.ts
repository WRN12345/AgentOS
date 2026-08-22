/** Agent 建议中心的类型/状态展示元数据（10.1 节六个 Agent + echo 占位能力 + requirement_pipeline 组合能力）。 */

/** suggestion_type → 中文标签与 Badge 样式。 */
export const SUGGESTION_TYPE_META: Record<
  string,
  { label: string; className: string }
> = {
  requirement: { label: "需求整理", className: "bg-blue-100 text-blue-700" },
  assignment: { label: "分配建议", className: "bg-violet-100 text-violet-700" },
  planning: { label: "规划建议", className: "bg-cyan-100 text-cyan-700" },
  risk: { label: "风险提示", className: "bg-red-100 text-red-700" },
  review: { label: "初审清单", className: "bg-amber-100 text-amber-700" },
  summary: { label: "进展摘要", className: "bg-green-100 text-green-700" },
  pipeline: { label: "需求拆解方案", className: "bg-indigo-100 text-indigo-700" },
  dev_doc_review: { label: "文档初审", className: "bg-teal-100 text-teal-700" },
  memory_proposal: {
    label: "核心记忆提议",
    className: "bg-orange-100 text-orange-700",
  },
  echo: { label: "链路自检", className: "bg-gray-100 text-gray-700" },
};

export function suggestionTypeLabel(type: string): string {
  return SUGGESTION_TYPE_META[type]?.label ?? type;
}

/** 人工采纳结果（review_status）展示。 */
export const REVIEW_STATUS_META: Record<
  string,
  { label: string; className: string }
> = {
  pending: { label: "待反馈", className: "bg-gray-100 text-gray-700" },
  accepted: { label: "已采纳", className: "bg-green-100 text-green-700" },
  ignored: { label: "已忽略", className: "bg-gray-200 text-gray-500" },
  // 核心记忆提议挂起超 7 天自动过期（16.6）：终态，不可再确认
  expired: { label: "已过期", className: "bg-gray-200 text-gray-500" },
};

/** 核心记忆提议动作（M4.4/M4.6，设计文档第 8 节）。 */
export const MEMORY_PROPOSAL_ACTION_LABELS: Record<string, string> = {
  create: "新增条目",
  update: "修改条目",
  deprecate: "作废条目",
  consolidate: "整合精简",
};

/** agent_runs.status 展示。 */
export const RUN_STATUS_META: Record<
  string,
  { label: string; className: string }
> = {
  pending: { label: "排队中", className: "bg-gray-100 text-gray-700" },
  running: { label: "运行中", className: "bg-blue-100 text-blue-700" },
  succeeded: { label: "成功", className: "bg-green-100 text-green-700" },
  failed: { label: "失败", className: "bg-red-100 text-red-700" },
};

/** agent_type → 中文标签（运行记录列表用）。 */
export const AGENT_TYPE_LABELS: Record<string, string> = {
  requirement_analyst: "需求分析",
  assignment_advisor: "分配顾问",
  planning_advisor: "规划顾问",
  workflow_risk: "风险扫描",
  deliverable_review: "交付初审",
  summary_agent: "进展摘要",
  requirement_pipeline: "需求拆解流水线",
  dev_doc_review: "文档初审",
  echo: "链路自检",
};

export function agentTypeLabel(agentType: string): string {
  return AGENT_TYPE_LABELS[agentType] ?? agentType;
}
