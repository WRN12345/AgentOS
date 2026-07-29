import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

vi.mock("../services/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../services/api")>();
  const { mockApi } = await import("../test/mock-api");
  return { ...actual, api: mockApi };
});

import DashboardPage from "../features/dashboard/DashboardPage";
import WorkItemsPage from "../features/work-items/WorkItemsPage";
import MembersPage from "../features/members/MembersPage";
import { CollaborationSection } from "../features/collaboration/CollaborationSection";
import { DeliverableSection } from "../features/deliverables/DeliverableSection";
import { mockApi, stubGet } from "../test/mock-api";
import { renderWithProviders, signInAs } from "../test/render";
import {
  makeLeader,
  makeMember,
  makeWorkItem,
  makeWorkItemSummary,
  memberBrief,
} from "../test/fixtures";
import type { CollaborationRequestSummary } from "../types";

/**
 * 成员核心路径页面集成测试（18.2 节 / T6.2）：
 * 团队看板 / 我的待处理 → 发起协作 → 提交交付，
 * 并断言权限差异：成员看不到负责人专属入口（创建工作项、新建成员、审批、时间线）。
 */

const leader = makeLeader();
const me = makeMember({ id: "member-1", display_name: "爱丽丝" });
const bob = makeMember({
  id: "member-2",
  display_name: "鲍勃",
  username: "bob",
  user_id: "user-2",
});
const members = [leader, me, bob];

// 我是主执行人的进行中工作项
const myWorkItem = makeWorkItem({
  id: "wi-1",
  title: "搭建 RAG 检索管道",
  status: "IN_PROGRESS",
  assignee: { id: me.id, display_name: me.display_name },
});

const receivedCollab: CollaborationRequestSummary = {
  id: "cr-1",
  work_item_id: "wi-1",
  work_item_title: "搭建 RAG 检索管道",
  requester: memberBrief({ id: bob.id, display_name: "鲍勃" }),
  assignee: memberBrief({ id: me.id, display_name: "爱丽丝" }),
  title: "补充评测语料",
  status: "REQUESTED",
  due_at: null,
  version: 1,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

describe("成员核心路径", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("第 1 步：团队看板聚合我的待处理（协作待响应 + 待开始任务），且不渲染负责人时间线", async () => {
    signInAs(me);
    const readyItem = makeWorkItemSummary({
      id: "wi-2",
      title: "编写评测脚本",
      status: "READY",
      assignee: memberBrief({ id: me.id, display_name: "爱丽丝" }),
    });
    stubGet({
      "/members": members,
      "/work-items": [myWorkItem, readyItem],
      "/collaboration-requests?role=received": [receivedCollab],
      "/collaboration-requests?role=sent": [],
      "/transfer-requests?role=mine": [],
      "/deadline-change-requests?role=mine": [],
    });

    renderWithProviders(<DashboardPage />);

    // 我的待处理：收到的协作请求（待响应）+ 我的 READY 工作项（待开始任务）
    expect(await screen.findByText("我的待处理")).toBeInTheDocument();
    expect(screen.getByText("待响应")).toBeInTheDocument();
    expect(screen.getByText(/协作「补充评测语料」/)).toBeInTheDocument();
    expect(screen.getByText("待开始任务")).toBeInTheDocument();
    expect(screen.getByText(/工作项「编写评测脚本」/)).toBeInTheDocument();

    // 看板状态分布：进行中 1、待开始 1
    expect(screen.getByText("全员工作量")).toBeInTheDocument();

    // 权限差异：项目时间线（审计事件流）仅负责人可见
    expect(screen.queryByText("项目时间线")).not.toBeInTheDocument();
  });

  it("第 2 步：发起协作（POST /work-items/{id}/collaboration-requests）", async () => {
    const user = userEvent.setup();
    signInAs(me);
    stubGet({ "/work-items/wi-1/collaboration-requests": [] });
    mockApi.post.mockResolvedValue({});

    renderWithProviders(
      <CollaborationSection workItem={myWorkItem} members={members} />,
    );
    await user.click(
      await screen.findByRole("button", { name: /发起协作/ }),
    );

    await user.click(screen.getByRole("combobox"));
    await user.click(await screen.findByRole("option", { name: "鲍勃" }));
    await user.type(screen.getByLabelText("标题"), "补充评测语料");
    await user.type(screen.getByLabelText("协作目标"), "提供 100 条标注语料");
    await user.click(screen.getByRole("button", { name: "发起" }));

    await waitFor(() => {
      expect(mockApi.post).toHaveBeenCalledWith(
        "/work-items/wi-1/collaboration-requests",
        expect.objectContaining({
          assignee_id: "member-2",
          title: "补充评测语料",
          goal: "提供 100 条标注语料",
        }),
        expect.any(String),
      );
    });
  });

  it("第 3 步：提交交付（POST /work-items/{id}/deliverables，Git 链接类型）", async () => {
    const user = userEvent.setup();
    signInAs(me);
    stubGet({
      "/work-items/wi-1/deliverables": [],
      "/work-items/wi-1/reviews": [],
    });
    mockApi.post.mockResolvedValue({ version: 1 });

    renderWithProviders(<DeliverableSection workItem={myWorkItem} />);
    await user.click(
      await screen.findByRole("button", { name: /提交交付/ }),
    );

    await user.type(
      screen.getByLabelText("Git 链接"),
      "https://github.com/org/repo/pull/42",
    );
    await user.click(screen.getByRole("button", { name: "提交" }));

    await waitFor(() => {
      expect(mockApi.post).toHaveBeenCalledWith(
        "/work-items/wi-1/deliverables",
        {
          type: "git_link",
          content: "https://github.com/org/repo/pull/42",
        },
        expect.any(String),
      );
    });
  });

  it("权限差异：成员在工作项列表看不到「创建工作项」，成员页看不到「新建成员」与管理操作", async () => {
    signInAs(me);
    stubGet({
      "/members": members,
      "/work-items": [myWorkItem],
      "/config": { llm_provider: "ollama", llm_is_external: false },
    });

    const { unmount } = renderWithProviders(<WorkItemsPage />);
    expect(
      await screen.findByText("搭建 RAG 检索管道"),
    ).toBeInTheDocument();
    // 负责人专属操作：创建工作项 / AI 需求引导 均不可见
    expect(
      screen.queryByRole("button", { name: /创建工作项/ }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /AI 需求引导/ }),
    ).not.toBeInTheDocument();
    unmount();

    renderWithProviders(<MembersPage />);
    expect(await screen.findByText("爱丽丝")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /新建成员/ }),
    ).not.toBeInTheDocument();
    // 负责人专属的编辑/禁用/确认能力操作列不可见；成员仍可填报自己的能力
    expect(
      screen.queryByRole("button", { name: /编辑/ }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /禁用/ }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "填报我的能力" }),
    ).toBeInTheDocument();
  });

  it("权限差异：成员不是主执行人时看不到「提交交付」", () => {
    signInAs(bob);
    stubGet({
      "/work-items/wi-1/deliverables": [],
      "/work-items/wi-1/reviews": [],
    });
    renderWithProviders(<DeliverableSection workItem={myWorkItem} />);
    expect(
      screen.queryByRole("button", { name: /提交交付/ }),
    ).not.toBeInTheDocument();
  });
});
