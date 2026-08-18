/**
 * 审计动作/目标类型中文映射。
 *
 * 项目时间线（负责人）与管理控制台（admin）共用同一套留痕标签，
 * 集中在此避免两处重复定义后漂移；新增后端 action 时只改这里。
 */
export const ACTION_LABELS: Record<string, string> = {
  "work_item.created": "创建工作项",
  "work_item.updated": "更新工作项",
  "work_item.published": "发布工作项",
  "work_item.started": "开始工作项",
  "work_item.blocked": "标记阻塞",
  "work_item.unblocked": "解除阻塞",
  "work_item.submitted": "提交审核",
  "work_item.cancelled": "取消工作项",
  "work_item.assignee_changed": "变更主执行人",
  "collaboration.requested": "发起协作请求",
  "collaboration.accepted": "接受协作请求",
  "collaboration.declined": "拒绝协作请求",
  "collaboration.started": "开始处理协作",
  "collaboration.submitted": "回传协作产物",
  "collaboration.revision_requested": "要求修改协作产物",
  "collaboration.completed": "完成协作请求",
  "collaboration.cancelled": "取消协作请求",
  "transfer.requested": "申请转派",
  "transfer.approved": "通过转派",
  "transfer.rejected": "驳回转派",
  "transfer.cancelled": "取消转派申请",
  "deadline_change.requested": "申请 DDL 变更",
  "deadline_change.approved": "通过 DDL 变更",
  "deadline_change.rejected": "驳回 DDL 变更",
  "deadline_change.cancelled": "取消 DDL 变更申请",
  "member.created": "新增成员",
  "member.updated": "更新成员",
  "member.capabilities.submitted": "提交能力标签",
  "member.capabilities.confirmed": "确认能力标签",
  "project.created": "创建项目",
  "user.updated": "账号启用/禁用",
};

export const TARGET_TYPE_LABELS: Record<string, string> = {
  work_item: "工作项",
  collaboration_request: "协作请求",
  transfer_request: "转派申请",
  deadline_change_request: "DDL 变更",
  project_member: "成员",
  project: "项目",
  user: "账号",
};
