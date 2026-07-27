import type {
  CollaborationStatus,
  DeadlineChangeStatus,
  TransferStatus,
} from "../../types";

/** 协作请求状态中文文案与徽标样式（8.2 节）。 */
export const COLLAB_STATUS_META: Record<
  CollaborationStatus,
  { label: string; className: string }
> = {
  REQUESTED: { label: "待响应", className: "bg-blue-100 text-blue-700" },
  ACCEPTED: { label: "已接受", className: "bg-cyan-100 text-cyan-700" },
  DECLINED: { label: "已拒绝", className: "bg-muted text-muted-foreground" },
  IN_PROGRESS: { label: "进行中", className: "bg-amber-100 text-amber-700" },
  SUBMITTED: { label: "已回传", className: "bg-purple-100 text-purple-700" },
  REVISION_REQUESTED: {
    label: "待修改",
    className: "bg-orange-100 text-orange-700",
  },
  COMPLETED: { label: "已完成", className: "bg-green-100 text-green-700" },
  CANCELLED: {
    label: "已取消",
    className: "bg-muted text-muted-foreground line-through",
  },
};

/** 转派申请状态中文文案与徽标样式（8.3 节）。 */
export const TRANSFER_STATUS_META: Record<
  TransferStatus,
  { label: string; className: string }
> = {
  PENDING: { label: "待审批", className: "bg-blue-100 text-blue-700" },
  APPROVED: { label: "已通过", className: "bg-green-100 text-green-700" },
  REJECTED: { label: "已驳回", className: "bg-red-100 text-red-700" },
  CANCELLED: {
    label: "已取消",
    className: "bg-muted text-muted-foreground line-through",
  },
};

/** DDL 变更申请状态中文文案与徽标样式（8.4 节）。 */
export const DEADLINE_CHANGE_STATUS_META: Record<
  DeadlineChangeStatus,
  { label: string; className: string }
> = {
  PENDING_IMPACT_ANALYSIS: {
    label: "影响分析中",
    className: "bg-cyan-100 text-cyan-700",
  },
  PENDING_APPROVAL: { label: "待审批", className: "bg-blue-100 text-blue-700" },
  APPROVED: { label: "已通过", className: "bg-green-100 text-green-700" },
  REJECTED: { label: "已驳回", className: "bg-red-100 text-red-700" },
  CANCELLED: {
    label: "已取消",
    className: "bg-muted text-muted-foreground line-through",
  },
};
