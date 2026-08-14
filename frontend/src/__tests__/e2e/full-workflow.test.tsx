import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

vi.mock("../../services/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../services/api")>();
  const { mockApi } = await import("../../test/mock-api");
  return { ...actual, api: mockApi };
});

import LoginPage from "../../features/auth/LoginPage";
import WorkItemsPage from "../../features/work-items/WorkItemsPage";
import { CollaborationSection } from "../../features/collaboration/CollaborationSection";
import { DeliverableSection } from "../../features/deliverables/DeliverableSection";
import { DeliveryReviewSection } from "../../features/approvals/DeliveryReviewSection";
import { useAuthStore } from "../../app/store";
import { mockApi, stubGet } from "../../test/mock-api";
import { renderWithProviders, signInAs } from "../../test/render";
import {
  makeDeliverable,
  makeLeader,
  makeMember,
  makeProject,
  makeUser,
  makeWorkItem,
  makeWorkItemSummary,
  memberBrief,
  tokens,
} from "../../test/fixtures";

/**
 * ============================================================================
 * 端到端验收场景骨架（设计文档 18.2 节，T6.2 / T6.3 共用）
 * ============================================================================
 *
 * 场景步骤（与后端 pytest 端到端场景 T6.3 一一对应，共用同一步骤编号）：
 *
 *   步骤 1  登录        POST /auth/login → GET /auth/me/projects（ticket 09：
 *                      24h 记忆窗口内自动进入上次项目，否则进项目选择页）
 *                      → GET /auth/me → GET /members
 *   步骤 2  分配        POST /work-items（负责人创建并指派主执行人，创建后为 DRAFT）
 *                      → POST /work-items/{id}/publish（发布，DRAFT → READY）
 *                      → 主执行人 POST /work-items/{id}/start（READY → IN_PROGRESS）
 *   步骤 3  转派        POST /work-items/{id}/transfer-requests（成员发起）
 *                      → POST /transfer-requests/{id}/approve（负责人审批通过，
 *                        工作项主执行人变更；两接口均带 version 乐观锁 + Idempotency-Key）
 *   步骤 4  协作        POST /work-items/{id}/collaboration-requests（主执行人发起）
 *                      → POST /collaboration-requests/{id}/accept（接收人接受）
 *                      → POST /collaboration-requests/{id}/start（接收人开始处理）
 *                      → POST /collaboration-requests/{id}/submit（回传产物）
 *                      → POST /collaboration-requests/{id}/complete（发起人确认完成）
 *   步骤 5  提交交付    POST /work-items/{id}/deliverables（主执行人提交，
 *                        每次提交版本号 +1，旧版本保留）
 *                      → POST /work-items/{id}/submit（工作项进入 IN_REVIEW）
 *   步骤 6  审核        POST /work-items/{id}/reviews（负责人结论 approve /
 *                        request_changes / reject；approve 后工作项 COMPLETED）
 *   步骤 7  归档        已完成工作项保留只读（状态 COMPLETED）；
 *                        或 POST /work-items/{id}/cancel 作废（CANCELLED）。
 *                        归档后审计链可通过 GET /audit-events（仅负责人）完整回放。
 *
 * 约定：本文件以页面级集成形式跑通「不依赖真实后端事务语义」的步骤
 * （API 层 mock）；依赖真实后端状态机/审计/通知/幂等语义的步骤以 it.todo
 * 占位，由后端 pytest 场景（T6.3）实现并断言，前端骨架只固定步骤与接口约定。
 * ============================================================================
 */

const leader = makeLeader();
const leaderUser = makeUser({
  id: leader.user_id,
  username: leader.username,
});
const alice = makeMember({ id: "member-1", display_name: "爱丽丝" });
const bob = makeMember({
  id: "member-2",
  display_name: "鲍勃",
  username: "bob",
  user_id: "user-2",
});
const members = [leader, alice, bob];

// 贯穿场景的工作项：爱丽丝为主执行人、进行中
const scenarioWorkItem = makeWorkItem({
  id: "wi-1",
  title: "搭建 RAG 检索管道",
  status: "IN_PROGRESS",
  assignee: { id: alice.id, display_name: alice.display_name },
});

describe("端到端场景：登录 → 分配 → 转派 → 协作 → 提交 → 审核 → 归档", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("步骤 1（可跑）：负责人登录，24h 记忆内自动进入上次项目并加载成员身份", async () => {
    const user = userEvent.setup();
    mockApi.post.mockResolvedValue(tokens);
    mockApi.get.mockImplementation((path: string) => {
      if (path === "/auth/me/projects") return Promise.resolve([makeProject()]);
      if (path === "/auth/me") return Promise.resolve(leaderUser);
      if (path === "/members") return Promise.resolve(members);
      return Promise.reject(new Error(`未 mock 的 GET ${path}`));
    });
    // 模拟上次登录选过该项目（24h 记忆窗口内）→ 登录后自动进入工作台
    useAuthStore.getState().setCurrentProject(makeProject());

    renderWithProviders(<LoginPage />);
    await user.type(screen.getByLabelText("用户名"), "leader");
    await user.type(screen.getByLabelText("密码"), "leader-password");
    await user.click(screen.getByRole("button", { name: "登录" }));

    // 接口约定：POST /auth/login → GET /auth/me/projects →（24h 记忆内自动进入）→ GET /auth/me → GET /members
    await waitFor(() => {
      expect(mockApi.post).toHaveBeenCalledWith("/auth/login", {
        username: "leader",
        password: "leader-password",
      });
    });
    await waitFor(() => {
      const state = useAuthStore.getState();
      expect(state.accessToken).toBe(tokens.access_token);
      expect(state.currentProject?.id).toBe("project-1");
      expect(state.member?.role).toBe("leader");
    });
  });

  it("步骤 2（可跑）：负责人创建工作项并指派主执行人", async () => {
    const user = userEvent.setup();
    signInAs(leader, leaderUser);
    stubGet({
      "/members": members,
      "/work-items": [],
      "/config": { llm_provider: "ollama", llm_is_external: false },
    });
    mockApi.post.mockResolvedValue({});

    renderWithProviders(<WorkItemsPage />);
    await user.click(
      await screen.findByRole("button", { name: /创建任务/ }),
    );
    await user.type(screen.getByLabelText("标题"), "搭建 RAG 检索管道");
    const comboboxes = screen.getAllByRole("combobox");
    await user.click(comboboxes[1]);
    await user.click(await screen.findByRole("option", { name: "爱丽丝" }));
    await user.click(screen.getByRole("button", { name: "创建" }));

    // 接口约定：POST /work-items（创建后为 DRAFT；幂等键随请求携带）
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
  });

  // TODO(T6.3 后端 pytest)：发布后状态机流转 DRAFT → READY → IN_PROGRESS。
  // 接口约定：POST /work-items/{id}/publish、POST /work-items/{id}/start，
  // body 均为 { version }，携带 Idempotency-Key；断言状态迁移与审计事件。
  it.todo("步骤 2b：发布工作项并由主执行人开始（依赖真实后端状态机）");

  // TODO(T6.3 后端 pytest)：转派全流程。
  // 接口约定：POST /work-items/{id}/transfer-requests { to_member_id, reason,
  // impact_note } → 负责人 POST /transfer-requests/{id}/approve { version,
  // decision_note }；断言工作项 assignee 变更、通知与审计事件。
  it.todo("步骤 3：成员发起转派申请，负责人审批通过（依赖真实后端事务）");

  it("步骤 4（可跑）：主执行人发起协作请求", async () => {
    const user = userEvent.setup();
    signInAs(alice);
    stubGet({ "/work-items/wi-1/collaboration-requests": [] });
    mockApi.post.mockResolvedValue({});

    renderWithProviders(
      <CollaborationSection workItem={scenarioWorkItem} members={members} />,
    );
    await user.click(
      await screen.findByRole("button", { name: /发起协作/ }),
    );
    await user.click(screen.getByRole("combobox"));
    await user.click(await screen.findByRole("option", { name: "鲍勃" }));
    await user.type(screen.getByLabelText("标题"), "补充评测语料");
    await user.type(screen.getByLabelText("协作目标"), "提供 100 条标注语料");
    await user.click(screen.getByRole("button", { name: "发起" }));

    // 接口约定：POST /work-items/{id}/collaboration-requests
    await waitFor(() => {
      expect(mockApi.post).toHaveBeenCalledWith(
        "/work-items/wi-1/collaboration-requests",
        expect.objectContaining({
          assignee_id: "member-2",
          title: "补充评测语料",
        }),
        expect.any(String),
      );
    });
  });

  // TODO(T6.3 后端 pytest)：协作状态机 accept → start → submit → complete。
  // 接口约定：POST /collaboration-requests/{id}/{action}，body { version }
  // （submit 额外携带 result_text）；断言双方通知与版本号递增。
  it.todo("步骤 4b：协作请求接受、开始、回传、确认完成（依赖真实后端状态机）");

  it("步骤 5（可跑）：主执行人提交交付物（生成第 1 版）", async () => {
    const user = userEvent.setup();
    signInAs(alice);
    stubGet({
      "/work-items/wi-1/deliverables": [],
      "/work-items/wi-1/reviews": [],
    });
    mockApi.post.mockResolvedValue({ version: 1 });

    renderWithProviders(
      <DeliverableSection workItem={scenarioWorkItem} />,
    );
    await user.click(
      await screen.findByRole("button", { name: /提交交付/ }),
    );
    await user.type(
      screen.getByLabelText("Git 链接"),
      "https://github.com/org/repo/pull/42",
    );
    await user.click(screen.getByRole("button", { name: "提交" }));

    // 接口约定：POST /work-items/{id}/deliverables { type, content }
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

  // TODO(T6.3 后端 pytest)：交付物重复提交版本号 +1 且旧版本保留可查；
  // POST /work-items/{id}/submit 使工作项进入 IN_REVIEW。
  it.todo("步骤 5b：再次提交生成第 2 版并送审（依赖真实后端版本语义）");

  it("步骤 6（可跑）：负责人审核交付物并提交「通过」结论", async () => {
    const user = userEvent.setup();
    signInAs(leader, leaderUser);
    const inReviewItem = makeWorkItemSummary({
      id: "wi-1",
      status: "IN_REVIEW",
      title: "搭建 RAG 检索管道",
      assignee: memberBrief({ id: alice.id, display_name: "爱丽丝" }),
    });
    stubGet({
      "/work-items?status=IN_REVIEW": [inReviewItem],
      "/work-items/wi-1/deliverables": [makeDeliverable({ id: "del-1" })],
      "/work-items/wi-1/reviews": [],
    });
    mockApi.post.mockResolvedValue({});

    renderWithProviders(<DeliveryReviewSection />);
    await user.click(await screen.findByRole("button", { name: "审核" }));
    await user.click(
      await screen.findByRole("button", { name: "提交审核结论" }),
    );

    // 接口约定：POST /work-items/{id}/reviews { deliverable_id, decision, feedback }
    await waitFor(() => {
      expect(mockApi.post).toHaveBeenCalledWith(
        "/work-items/wi-1/reviews",
        { deliverable_id: "del-1", decision: "approve", feedback: null },
        expect.any(String),
      );
    });
  });

  // TODO(T6.3 后端 pytest)：approve 后工作项状态 COMPLETED（归档为只读），
  // 全流程审计事件可通过 GET /audit-events 按第 9 章时序完整回放；
  // 通知（GET /notifications）与 Agent 建议不改变正式业务状态（18.3 节）。
  it.todo("步骤 7：工作项归档（COMPLETED）与审计链回放（依赖真实后端）");
});
