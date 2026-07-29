import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

vi.mock("../services/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../services/api")>();
  const { mockApi } = await import("../test/mock-api");
  return { ...actual, api: mockApi };
});

import WorkItemsPage from "../features/work-items/WorkItemsPage";
import MembersPage from "../features/members/MembersPage";
import ApprovalsPage from "../features/approvals/ApprovalsPage";
import { stubGet } from "../test/mock-api";
import { renderWithProviders, signInAs } from "../test/render";
import {
  makeLeader,
  makeMember,
  makeWorkItemSummary,
  memberBrief,
} from "../test/fixtures";
import type { ApprovalItem } from "../types";

/**
 * 管理员（admin）角色页面可见性测试：
 * - 成员管理页：与 leader 同权（新建/编辑/禁用入口可见），角色徽标显示"管理员"；
 * - 业务写入口隐藏：工作项页看不到「创建工作项 / AI 需求引导」；
 * - 审批中心：可读「待我审批」列表，但没有通过/驳回操作入口。
 */

const leader = makeLeader();
const admin = makeMember({
  id: "member-admin",
  user_id: "user-admin",
  username: "admin",
  role: "admin",
  display_name: "王管理",
});
const alice = makeMember({ id: "member-1", display_name: "爱丽丝" });
const members = [leader, admin, alice];

const pendingApproval: ApprovalItem = {
  kind: "transfer",
  id: "tr-1",
  work_item_id: "wi-1",
  work_item_title: "搭建 RAG 检索管道",
  summary: "爱丽丝 → 爱丽丝",
  requested_by: memberBrief({ id: alice.id, display_name: "爱丽丝" }),
  status: "PENDING",
  impact_analysis_status: null,
  version: 1,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  from_member: memberBrief({ id: alice.id, display_name: "爱丽丝" }),
  to_member: memberBrief({ id: "member-2", display_name: "鲍勃" }),
  target_type: null,
  target_id: null,
  old_due_at: null,
  new_due_at: null,
};

describe("管理员角色页面可见性", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("成员管理页：admin 可见新建/编辑/禁用入口，角色徽标显示「管理员」", async () => {
    signInAs(admin);
    stubGet({ "/members": members });

    renderWithProviders(<MembersPage />);

    expect(await screen.findByText("王管理")).toBeInTheDocument();
    expect(screen.getByText("管理员")).toBeInTheDocument();
    // 与 leader 同权的账号管理入口
    expect(
      screen.getByRole("button", { name: /新建成员/ }),
    ).toBeInTheDocument();
    expect(
      screen.getAllByRole("button", { name: /编辑/ }).length,
    ).toBeGreaterThan(0);
    expect(
      screen.getAllByRole("button", { name: /禁用/ }).length,
    ).toBeGreaterThan(0);
  });

  it("工作项页：admin 看不到「创建工作项 / AI 需求引导」业务写入口", async () => {
    signInAs(admin);
    stubGet({
      "/work-items": [makeWorkItemSummary()],
      "/config": { llm_provider: "ollama", llm_is_external: false },
    });

    renderWithProviders(<WorkItemsPage />);

    expect(await screen.findByText("RAG 检索管道")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /创建工作项/ }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /AI 需求引导/ }),
    ).not.toBeInTheDocument();
  });

  it("审批中心：admin 可读「待我审批」列表，但没有通过/驳回按钮与交付审核页签", async () => {
    signInAs(admin);
    stubGet({
      "/approvals": [pendingApproval],
      "/transfer-requests?role=mine": [],
      "/deadline-change-requests?role=mine": [],
    });

    renderWithProviders(<ApprovalsPage />);

    // 待审批列表只读可见
    expect(await screen.findByText("搭建 RAG 检索管道")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "待我审批" })).toBeInTheDocument();
    // 无审批操作入口（负责人专属）
    expect(
      screen.queryByRole("button", { name: "通过" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "驳回" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("tab", { name: "交付审核" }),
    ).not.toBeInTheDocument();
  });
});
