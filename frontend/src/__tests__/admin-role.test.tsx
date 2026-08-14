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
  makeUser,
  makeWorkItemSummary,
  memberBrief,
} from "../test/fixtures";
import type { ApprovalItem } from "../types";

/**
 * 全局管理员（users.is_admin）页面可见性测试：
 * - 管理员不属于任何项目成员，但可管理成员账号（useCanManageMembers 放行）；
 * - 业务写入口按项目负责人角色隐藏（管理员不是负责人）；
 * - 审批中心可读「待我审批」，但没有通过/驳回操作入口；
 * - 成员列表不再出现「管理员」角色行（管理员为全局角色）。
 */

const leader = makeLeader();
const alice = makeMember({ id: "member-1", display_name: "爱丽丝" });
const members = [leader, alice];

/** 全局管理员：users.is_admin=true，无项目成员记录。 */
const adminUser = makeUser({
  id: "user-admin",
  username: "admin",
  is_admin: true,
});

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

describe("全局管理员页面可见性", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("成员管理页：admin 可见账号管理入口，成员列表不再有「管理员」角色行", async () => {
    signInAs(null, adminUser);
    stubGet({ "/members": members });

    renderWithProviders(<MembersPage />);

    expect(await screen.findByText("爱丽丝")).toBeInTheDocument();
    // 与 leader 同权的账号管理入口
    expect(
      screen.getByRole("button", { name: /新建成员/ }),
    ).toBeInTheDocument();
    expect(
      screen.getAllByRole("button", { name: /编辑/ }).length,
    ).toBeGreaterThan(0);
    // 管理员为全局角色，成员表不再渲染「管理员」行/徽标
    expect(screen.queryByText("管理员")).not.toBeInTheDocument();
  });

  it("成员管理页：leader 视角成员行操作按钮正常，且无管理员行", async () => {
    signInAs(leader);
    stubGet({ "/members": members });

    renderWithProviders(<MembersPage />);

    expect(await screen.findByText("爱丽丝")).toBeInTheDocument();
    // 仅 leader 本人行与爱丽丝行有操作按钮
    expect(screen.getAllByRole("button", { name: /编辑/ })).toHaveLength(2);
    expect(screen.getAllByRole("button", { name: /禁用/ })).toHaveLength(2);
  });

  it("工作项页：admin 不是负责人，看不到「创建工作项 / AI 需求引导」", async () => {
    signInAs(null, adminUser);
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
    signInAs(null, adminUser);
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
