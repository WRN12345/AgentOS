import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { Badge } from "@/components/ui/badge";
import type { WorkItemPriority, WorkItemStatus } from "../../../types";
import { PRIORITY_META, STATUS_META } from "../constants";
import {
  COLLAB_STATUS_META,
  DEADLINE_CHANGE_STATUS_META,
  TRANSFER_STATUS_META,
} from "../../collaboration/constants";
import { REVIEW_DECISION_META } from "../../deliverables/constants";

/**
 * 状态徽标测试（18.2 节）：工作项/审批各状态枚举的中文文案映射完整，
 * 且以 Badge 渲染时携带对应样式类。
 */
describe("工作项状态徽标", () => {
  const expected: Record<WorkItemStatus, string> = {
    DRAFT: "草稿",
    READY: "待开始",
    IN_PROGRESS: "进行中",
    BLOCKED: "阻塞",
    IN_REVIEW: "审核中",
    COMPLETED: "已完成",
    CANCELLED: "已取消",
  };

  it.each(Object.entries(expected))(
    "状态 %s 渲染中文文案「%s」与样式类",
    (status, label) => {
      const meta = STATUS_META[status as WorkItemStatus];
      render(<Badge className={meta.className}>{meta.label}</Badge>);
      const badge = screen.getByText(label);
      expect(badge).toBeInTheDocument();
      expect(badge.className).toContain(meta.className.split(" ")[0]);
    },
  );

  it("STATUS_META 覆盖全部 7 种工作项状态", () => {
    expect(Object.keys(STATUS_META).sort()).toEqual(
      Object.keys(expected).sort(),
    );
  });
});

describe("优先级徽标", () => {
  it.each([
    ["low", "低"],
    ["medium", "中"],
    ["high", "高"],
    ["urgent", "紧急"],
  ] as [WorkItemPriority, string][])("优先级 %s 渲染「%s」", (p, label) => {
    render(
      <Badge className={PRIORITY_META[p].className}>
        {PRIORITY_META[p].label}
      </Badge>,
    );
    expect(screen.getByText(label)).toBeInTheDocument();
  });
});

describe("审批状态徽标", () => {
  it("转派申请状态文案映射完整", () => {
    expect(TRANSFER_STATUS_META.PENDING.label).toBe("待审批");
    expect(TRANSFER_STATUS_META.APPROVED.label).toBe("已通过");
    expect(TRANSFER_STATUS_META.REJECTED.label).toBe("已驳回");
    expect(TRANSFER_STATUS_META.CANCELLED.label).toBe("已取消");
  });

  it("DDL 变更申请状态文案映射完整", () => {
    expect(DEADLINE_CHANGE_STATUS_META.PENDING_IMPACT_ANALYSIS.label).toBe(
      "影响分析中",
    );
    expect(DEADLINE_CHANGE_STATUS_META.PENDING_APPROVAL.label).toBe("待审批");
    expect(DEADLINE_CHANGE_STATUS_META.APPROVED.label).toBe("已通过");
    expect(DEADLINE_CHANGE_STATUS_META.REJECTED.label).toBe("已驳回");
    expect(DEADLINE_CHANGE_STATUS_META.CANCELLED.label).toBe("已取消");
  });

  it("审核结论文案映射完整", () => {
    expect(REVIEW_DECISION_META.approve.label).toBe("通过");
    expect(REVIEW_DECISION_META.request_changes.label).toBe("要求修改");
    expect(REVIEW_DECISION_META.reject.label).toBe("拒绝");
  });

  it("协作请求状态徽标渲染", () => {
    render(
      <Badge className={COLLAB_STATUS_META.SUBMITTED.className}>
        {COLLAB_STATUS_META.SUBMITTED.label}
      </Badge>,
    );
    expect(screen.getByText("已回传")).toBeInTheDocument();
  });
});
