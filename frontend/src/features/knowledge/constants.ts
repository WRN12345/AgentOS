import type { IndexStatus } from "../../types";

/** 索引状态展示元信息（设计文档第 6 节）。 */
export const INDEX_STATUS_META: Record<
  IndexStatus,
  { label: string; variant: "default" | "secondary" | "destructive" | "outline" }
> = {
  pending: { label: "待处理", variant: "outline" },
  indexing: { label: "索引中", variant: "secondary" },
  indexed: { label: "已索引", variant: "default" },
  failed: { label: "失败", variant: "destructive" },
  unindexed: { label: "未索引", variant: "outline" },
};
