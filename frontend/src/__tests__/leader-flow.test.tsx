import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

vi.mock("../services/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../services/api")>();
  const { mockApi } = await import("../test/mock-api");
  return { ...actual, api: mockApi };
});

import { toast } from "sonner";
import MembersPage from "../features/members/MembersPage";
import WorkItemsPage from "../features/work-items/WorkItemsPage";
import ApprovalsPage from "../features/approvals/ApprovalsPage";
import { DeliveryReviewSection } from "../features/approvals/DeliveryReviewSection";
import { mockApi, stubGet } from "../test/mock-api";
import { renderWithProviders, signInAs } from "../test/render";
import {
  makeDeliverable,
  makeLeader,
  makeMember,
  makeWorkItemSummary,
  memberBrief,
} from "../test/fixtures";
import type { ApprovalItem } from "../types";

/**
 * 负责人核心路径页面集成测试（18.2 节 / T6.2）：
 * 创建成员 → 创建工作项（分配）→ 审批 DDL 变更 → 审核交付物。
 * API 层整体 mock（src/services/api 的 api 对象），页面按真实 Provider 组合挂载。
 */

const leader = makeLeader();
const alice = makeMember({ id: "member-1", display_name: "爱丽丝" });

const deadlineApproval: ApprovalItem = {
  kind: "deadline_change",
  id: "dc-1",
  work_item_id: "wi-1",
  work_item_title: "RAG 检索管道",
  summary: "申请将主任务截止时间顺延至 2026-08-10",
  requested_by: memberBrief({ id: alice.id, display_name: "爱丽丝" }),
  status: "PENDING_APPROVAL",
  impact_analysis_status: "unavailable",
  version: 2,
  created_at: "2026-01-02T00:00:00Z",
  updated_at: "2026-01-02T00:00:00Z",
  from_member: null,
  to_member: null,
  target_type: "work_item",
  target_id: "wi-1",
  old_due_at: "2026-08-03T00:00:00Z",
  new_due_at: "2026-08-10T00:00:00Z",
};

describe("负责人核心路径", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("第 1 步：添加已有成员（POST /members，仅 username），复用账号无初始密码", async () => {
    const user = userEvent.setup();
    signInAs(leader);
    stubGet({ "/members": [leader] });
    mockApi.post.mockResolvedValue({
      ...alice,
      id: "member-new",
      username: "alice",
    });

    renderWithProviders(<MembersPage />);
    // 负责人可见「添加成员」入口（建号收敛到 admin，仅能复用已有账号）
    await user.click(
      await screen.findByRole("button", { name: /添加成员/ }),
    );

    await user.type(screen.getByLabelText("用户名"), "alice");
    await user.click(screen.getByRole("button", { name: "添加" }));

    await waitFor(() => {
      expect(mockApi.post).toHaveBeenCalledWith(
        "/members",
        expect.objectContaining({ username: "alice" }),
        expect.any(String),
      );
    });
    // 复用已有账号，无初始密码
    expect(
      await screen.findByText("（复用已有账号，无初始密码）"),
    ).toBeInTheDocument();
  });

  it("第 2 步：创建工作项并指派主执行人（POST /work-items）", async () => {
    const user = userEvent.setup();
    signInAs(leader);
    stubGet({
      "/members": [leader, alice],
      "/work-items": [],
      "/config": { llm_provider: "ollama", llm_is_external: false },
    });
    mockApi.post.mockResolvedValue({});

    renderWithProviders(<WorkItemsPage />);
    // 负责人可见「创建任务」入口（成员不可见，见成员路径测试）
    await user.click(
      await screen.findByRole("button", { name: /创建任务/ }),
    );

    await user.type(screen.getByLabelText("标题"), "搭建 RAG 检索管道");
    const comboboxes = screen.getAllByRole("combobox");
    // 对话框内第二个 Select 是主执行人（第一个是优先级）
    await user.click(comboboxes[1]);
    await user.click(
      await screen.findByRole("option", { name: "爱丽丝" }),
    );
    await user.click(screen.getByRole("button", { name: "创建" }));

    await waitFor(() => {
      expect(mockApi.post).toHaveBeenCalledWith(
        "/work-items",
        expect.objectContaining({
          title: "搭建 RAG 检索管道",
          assignee_id: "member-1",
        }),
        expect.any(String),
      );
    });
    await waitFor(() => {
      expect(toast.success).toHaveBeenCalledWith("工作项已创建");
    });
  });

  it("第 3 步：审批中心通过 DDL 变更（POST /deadline-change-requests/{id}/approve）", async () => {
    const user = userEvent.setup();
    signInAs(leader);
    stubGet({ "/approvals": [deadlineApproval] });
    mockApi.post.mockResolvedValue({});

    renderWithProviders(<ApprovalsPage />);
    // 等待卡片渲染后点击「通过」
    const card = (await screen.findByText("DDL 变更")).closest(
      "[data-slot='card']",
    ) as HTMLElement;
    await user.click(within(card).getByRole("button", { name: "通过" }));
    await user.click(
      await screen.findByRole("button", { name: "确认通过" }),
    );

    await waitFor(() => {
      expect(mockApi.post).toHaveBeenCalledWith(
        "/deadline-change-requests/dc-1/approve",
        { version: 2, decision_note: null },
        expect.any(String),
      );
    });
  });

  it("第 4 步：交付审核——查看交付物并提交「通过」结论", async () => {
    const user = userEvent.setup();
    signInAs(leader);
    const inReviewItem = makeWorkItemSummary({
      id: "wi-1",
      status: "IN_REVIEW",
      title: "搭建 RAG 检索管道",
      assignee: memberBrief({ id: alice.id, display_name: "爱丽丝" }),
    });
    const deliverable = makeDeliverable({ id: "del-1", version: 2 });
    stubGet({
      "/work-items?status=IN_REVIEW": [inReviewItem],
      "/work-items/wi-1/deliverables": [deliverable],
      "/work-items/wi-1/reviews": [],
    });
    mockApi.post.mockResolvedValue({});

    renderWithProviders(<DeliveryReviewSection />);
    await user.click(await screen.findByRole("button", { name: "审核" }));

    // 审核对话框展示交付物内容（Git 链接）
    expect(
      await screen.findByText("https://github.com/org/repo/pull/1"),
    ).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "提交审核结论" }),
    );
    await waitFor(() => {
      expect(mockApi.post).toHaveBeenCalledWith(
        "/work-items/wi-1/reviews",
        {
          deliverable_id: "del-1",
          decision: "approve",
          feedback: null,
        },
        expect.any(String),
      );
    });
    await waitFor(() => {
      expect(toast.success).toHaveBeenCalledWith("已提交审核结论：通过");
    });
  });

  it("第 4 步补充：「要求修改」必须填写反馈，否则前端拦截不调用接口", async () => {
    const user = userEvent.setup();
    signInAs(leader);
    const inReviewItem = makeWorkItemSummary({ id: "wi-1", status: "IN_REVIEW" });
    stubGet({
      "/work-items?status=IN_REVIEW": [inReviewItem],
      "/work-items/wi-1/deliverables": [makeDeliverable()],
      "/work-items/wi-1/reviews": [],
    });

    renderWithProviders(<DeliveryReviewSection />);
    await user.click(await screen.findByRole("button", { name: "审核" }));
    await user.click(
      await screen.findByRole("button", { name: "要求修改" }),
    );
    await user.click(
      screen.getByRole("button", { name: "提交审核结论" }),
    );

    expect(
      await screen.findByText("要求修改时必须填写反馈"),
    ).toBeInTheDocument();
    expect(mockApi.post).not.toHaveBeenCalled();
  });
});
