import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

vi.mock("../../../services/api", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("../../../services/api")>();
  const { mockApi } = await import("../../../test/mock-api");
  return { ...actual, api: mockApi };
});

import { toast } from "sonner";
import ApprovalsPage from "../ApprovalsPage";
import { mockApi, stubGet } from "../../../test/mock-api";
import { renderWithProviders, signInAs } from "../../../test/render";
import {
  makeLeader,
  makeMember,
  memberBrief,
} from "../../../test/fixtures";
import type { ApprovalItem } from "../../../types";

const leader = makeLeader();
const member = makeMember({ id: "member-1", display_name: "爱丽丝" });

const transferItem: ApprovalItem = {
  kind: "transfer",
  id: "tr-1",
  work_item_id: "wi-1",
  work_item_title: "RAG 检索管道",
  summary: "爱丽丝 申请将主执行人转派给 鲍勃",
  requested_by: memberBrief({ id: "member-1", display_name: "爱丽丝" }),
  status: "PENDING",
  impact_analysis_status: null,
  version: 4,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  from_member: memberBrief({ id: "member-1", display_name: "爱丽丝" }),
  to_member: memberBrief({ id: "member-2", display_name: "鲍勃" }),
  target_type: null,
  target_id: null,
  old_due_at: null,
  new_due_at: null,
};

const deadlineItem: ApprovalItem = {
  kind: "deadline_change",
  id: "dc-1",
  work_item_id: "wi-2",
  work_item_title: "评测报告",
  summary: "申请将主任务截止时间顺延至 2026-08-10",
  requested_by: memberBrief({ id: "member-1", display_name: "爱丽丝" }),
  status: "PENDING_APPROVAL",
  impact_analysis_status: "unavailable",
  version: 1,
  created_at: "2026-01-02T00:00:00Z",
  updated_at: "2026-01-02T00:00:00Z",
  from_member: null,
  to_member: null,
  target_type: "work_item",
  target_id: "wi-2",
  old_due_at: "2026-08-03T00:00:00Z",
  new_due_at: "2026-08-10T00:00:00Z",
};

/** 审批卡片组件测试（18.2 节）：渲染、通过/驳回调用 API、成员权限差异。 */
describe("ApprovalsPage 审批卡片", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("负责人可见「待我审批」并渲染转派与 DDL 变更卡片", async () => {
    signInAs(leader);
    stubGet({ "/approvals": [transferItem, deadlineItem] });
    renderWithProviders(<ApprovalsPage />);

    expect(screen.getByRole("tab", { name: "待我审批" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "交付审核" })).toBeInTheDocument();

    // 转派卡片
    expect(await screen.findByText("转派申请")).toBeInTheDocument();
    expect(screen.getByText("RAG 检索管道")).toBeInTheDocument();
    expect(
      screen.getByText("爱丽丝 申请将主执行人转派给 鲍勃"),
    ).toBeInTheDocument();
    // DDL 卡片：影响分析不可用时给出人工决策提示
    expect(screen.getByText("DDL 变更")).toBeInTheDocument();
    expect(
      screen.getByText(/未生成 AI 影响分析，请基于业务信息人工决策/),
    ).toBeInTheDocument();
  });

  it("点击「通过」并在对话框确认后调用 approve 接口（携带 version）", async () => {
    const user = userEvent.setup();
    signInAs(leader);
    stubGet({ "/approvals": [transferItem] });
    mockApi.post.mockResolvedValue({});

    renderWithProviders(<ApprovalsPage />);
    await user.click(
      await screen.findByRole("button", { name: "通过" }),
    );
    await user.click(
      await screen.findByRole("button", { name: "确认通过" }),
    );

    await waitFor(() => {
      expect(mockApi.post).toHaveBeenCalledWith(
        "/transfer-requests/tr-1/approve",
        { version: 4, decision_note: null },
        expect.any(String),
      );
    });
    await waitFor(() => {
      expect(toast.success).toHaveBeenCalledWith("已通过审批");
    });
  });

  it("点击「驳回」可填写审批意见并调用 reject 接口", async () => {
    const user = userEvent.setup();
    signInAs(leader);
    stubGet({ "/approvals": [transferItem] });
    mockApi.post.mockResolvedValue({});

    renderWithProviders(<ApprovalsPage />);
    await user.click(
      await screen.findByRole("button", { name: "驳回" }),
    );
    await user.type(screen.getByLabelText(/审批意见/), "当前负载已满，暂缓转派");
    await user.click(screen.getByRole("button", { name: "确认驳回" }));

    await waitFor(() => {
      expect(mockApi.post).toHaveBeenCalledWith(
        "/transfer-requests/tr-1/reject",
        { version: 4, decision_note: "当前负载已满，暂缓转派" },
        expect.any(String),
      );
    });
    await waitFor(() => {
      expect(toast.success).toHaveBeenCalledWith("已驳回申请");
    });
  });

  it("成员登录时看不到「待我审批/交付审核」入口，默认展示「我的申请」", async () => {
    signInAs(member);
    stubGet({
      "/transfer-requests?role=mine": [],
      "/deadline-change-requests?role=mine": [],
    });
    renderWithProviders(<ApprovalsPage />);

    expect(
      screen.queryByRole("tab", { name: "待我审批" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("tab", { name: "交付审核" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "我的申请" })).toBeInTheDocument();
    expect(
      await screen.findByText("我发起的转派、DDL 变更申请与提交的交付物会显示在这里"),
    ).toBeInTheDocument();
  });
});
