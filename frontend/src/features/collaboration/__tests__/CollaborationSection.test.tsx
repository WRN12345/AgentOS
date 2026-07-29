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
import { CollaborationSection } from "../CollaborationSection";
import { mockApi, stubGet } from "../../../test/mock-api";
import { renderWithProviders, signInAs } from "../../../test/render";
import {
  makeMember,
  makeWorkItem,
  memberBrief,
} from "../../../test/fixtures";
import type { CollaborationRequestSummary } from "../../../types";

const self = makeMember({ id: "member-1", display_name: "爱丽丝" });
const other = makeMember({
  id: "member-2",
  display_name: "鲍勃",
  username: "bob",
  user_id: "user-2",
});
const members = [self, other];

// 当前用户为主执行人的工作项
const workItem = makeWorkItem({
  id: "wi-1",
  assignee: { id: self.id, display_name: self.display_name },
});

const requestedCollab: CollaborationRequestSummary = {
  id: "cr-1",
  work_item_id: "wi-1",
  work_item_title: "RAG 检索管道",
  requester: memberBrief({ id: other.id, display_name: "鲍勃" }),
  assignee: memberBrief({ id: self.id, display_name: "爱丽丝" }),
  title: "补充评测语料",
  status: "REQUESTED",
  due_at: null,
  version: 2,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

/** 协作请求区组件测试（18.2 节）：列表渲染、身份相关操作、发起表单校验与提交。 */
describe("CollaborationSection 协作请求", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("渲染协作列表与状态徽标", async () => {
    signInAs(self);
    stubGet({
      "/work-items/wi-1/collaboration-requests": [requestedCollab],
    });
    renderWithProviders(
      <CollaborationSection workItem={workItem} members={members} />,
    );

    expect(await screen.findByText("补充评测语料")).toBeInTheDocument();
    expect(screen.getByText("待响应")).toBeInTheDocument();
    // 我（接收人）对待响应的请求可见 接受/拒绝
    expect(screen.getByRole("button", { name: "接受" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "拒绝" })).toBeInTheDocument();
  });

  it("「发起协作」按钮仅主执行人可见", async () => {
    stubGet({ "/work-items/wi-1/collaboration-requests": [] });

    signInAs(self);
    const { unmount } = renderWithProviders(
      <CollaborationSection workItem={workItem} members={members} />,
    );
    expect(
      await screen.findByRole("button", { name: /发起协作/ }),
    ).toBeInTheDocument();
    unmount();

    // 非主执行人（鲍勃）看不到发起入口
    signInAs(other);
    renderWithProviders(
      <CollaborationSection workItem={workItem} members={members} />,
    );
    expect(
      screen.queryByRole("button", { name: /发起协作/ }),
    ).not.toBeInTheDocument();
  });

  it("发起协作：必填校验后提交调用 POST /work-items/{id}/collaboration-requests", async () => {
    const user = userEvent.setup();
    signInAs(self);
    stubGet({ "/work-items/wi-1/collaboration-requests": [] });
    mockApi.post.mockResolvedValue({});

    renderWithProviders(
      <CollaborationSection workItem={workItem} members={members} />,
    );
    await user.click(
      await screen.findByRole("button", { name: /发起协作/ }),
    );

    // 空表单提交 → 三项必填校验
    await user.click(screen.getByRole("button", { name: "发起" }));
    expect(await screen.findByText("请选择接收人")).toBeInTheDocument();
    expect(screen.getByText("请输入标题")).toBeInTheDocument();
    expect(screen.getByText("请输入协作目标")).toBeInTheDocument();
    expect(mockApi.post).not.toHaveBeenCalled();

    // 填写完整（候选人排除自己，只剩鲍勃）
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
          template: null,
          due_at: null,
        }),
        expect.any(String),
      );
    });
    await waitFor(() => {
      expect(toast.success).toHaveBeenCalledWith("协作请求已发起");
    });
  });

  it("接收人点击「接受」调用 accept 命令（携带 version 乐观锁）", async () => {
    const user = userEvent.setup();
    signInAs(self);
    stubGet({
      "/work-items/wi-1/collaboration-requests": [requestedCollab],
    });
    mockApi.post.mockResolvedValue({});

    renderWithProviders(
      <CollaborationSection workItem={workItem} members={members} />,
    );
    await user.click(await screen.findByRole("button", { name: "接受" }));

    await waitFor(() => {
      expect(mockApi.post).toHaveBeenCalledWith(
        "/collaboration-requests/cr-1/accept",
        { version: 2 },
        expect.any(String),
      );
    });
  });
});
