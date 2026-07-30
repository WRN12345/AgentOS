import type { WorkItemPriority, WorkItemStatus, DevDocStatus } from "../../types";

/** 工作项状态中文文案与徽标样式。 */
export const STATUS_META: Record<
  WorkItemStatus,
  { label: string; className: string }
> = {
  DRAFT: { label: "草稿", className: "bg-muted text-muted-foreground" },
  READY: { label: "待开始", className: "bg-blue-100 text-blue-700" },
  IN_PROGRESS: { label: "进行中", className: "bg-amber-100 text-amber-700" },
  BLOCKED: { label: "阻塞", className: "bg-red-100 text-red-700" },
  IN_REVIEW: { label: "审核中", className: "bg-purple-100 text-purple-700" },
  COMPLETED: { label: "已完成", className: "bg-green-100 text-green-700" },
  CANCELLED: { label: "已取消", className: "bg-muted text-muted-foreground line-through" },
};

export const PRIORITY_META: Record<
  WorkItemPriority,
  { label: string; className: string }
> = {
  low: { label: "低", className: "bg-muted text-muted-foreground" },
  medium: { label: "中", className: "bg-blue-100 text-blue-700" },
  high: { label: "高", className: "bg-orange-100 text-orange-700" },
  urgent: { label: "紧急", className: "bg-red-100 text-red-700" },
};

/** 开发文档状态中文文案与徽标样式（2026-07-30 设计文档 §4.1）。 */
export const DEV_DOC_STATUS_META: Record<
  DevDocStatus,
  { label: string; className: string }
> = {
  DRAFT: { label: "草稿", className: "bg-muted text-muted-foreground" },
  SUBMITTED: { label: "待确认", className: "bg-blue-100 text-blue-700" },
  CONFIRMED: { label: "已确认", className: "bg-green-100 text-green-700" },
  RETURNED: { label: "已打回", className: "bg-red-100 text-red-700" },
};

/** 格式化 ISO 时间为本地日期时间字符串。 */
export function formatDateTime(value: string | null): string {
  if (!value) return "—";
  return new Date(value).toLocaleString("zh-CN", { hour12: false });
}

/** 格式化 ISO 时间为本地日期字符串。 */
export function formatDate(value: string | null): string {
  if (!value) return "—";
  return new Date(value).toLocaleDateString("zh-CN");
}
