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
import { DeadlineChangeSection } from "../DeadlineChangeSection";
import { mockApi, stubGet } from "../../../test/mock-api";
import { renderWithProviders, signInAs } from "../../../test/render";
import { makeMember, makeWorkItem, memberBrief } from "../../../test/fixtures";
import type { CollaborationRequestSummary } from "../../../types";

const assignee = makeMember({ id: "member-1", display_name: "爱丽丝" });
const bystander = makeMember({
  id: "member-3",
  display_name: "路人",
  username: "carol",
  user_id: "user-3",
});

const workItem = makeWorkItem({
  id: "wi-1",
  title: "RAG 检索管道",
  assignee: { id: assignee.id, display_name: assignee.display_name },
});

const activeCollab: CollaborationRequestSummary = {
  id: "cr-1",
  work_item_id: "wi-1",
  work_item_title: "RAG 检索管道",
  requester: memberBrief({ id: "member-2", display_name: "鲍勃" }),
  assignee: memberBrief({ id: assignee.id, display_name: "爱丽丝" }),
  title: "补充评测语料",
  status: "IN_PROGRESS",
  due_at: null,
  version: 1,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

/** DDL 变更区组件测试（18.2 节）：目标可见性（权限）、必填校验、提交调用 API。 */
describe("DeadlineChangeSection DDL 变更", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("无关成员（非主执行人/负责人，无进行中协作）看不到「申请变更」入口", () => {
    signInAs(bystander);
    stubGet({
      "/work-items/wi-1/deadline-change-requests": [],
      "/work-items/wi-1/collaboration-requests": [activeCollab],
    });
    renderWithProviders(<DeadlineChangeSection workItem={workItem} />);
    expect(
      screen.queryByRole("button", { name: /申请变更/ }),
    ).not.toBeInTheDocument();
  });

  it("主执行人可申请：目标含主任务与我参与的进行中协作", async () => {
    const user = userEvent.setup();
    signInAs(assignee);
    stubGet({
      "/work-items/wi-1/deadline-change-requests": [],
      "/work-items/wi-1/collaboration-requests": [activeCollab],
    });
    renderWithProviders(<DeadlineChangeSection workItem={workItem} />);

    await user.click(
      await screen.findByRole("button", { name: /申请变更/ }),
    );
    await user.click(screen.getByRole("combobox"));
    const options = await screen.findAllByRole("option");
    const texts = options.map((o) => o.textContent);
    expect(texts).toContain("主任务 DDL（RAG 检索管道）");
    expect(texts).toContain("协作 DDL（补充评测语料）");
  });

  it("必填校验：空表单提交展示三项错误且不调用接口", async () => {
    const user = userEvent.setup();
    signInAs(assignee);
    stubGet({
      "/work-items/wi-1/deadline-change-requests": [],
      "/work-items/wi-1/collaboration-requests": [],
    });
    renderWithProviders(<DeadlineChangeSection workItem={workItem} />);

    await user.click(
      await screen.findByRole("button", { name: /申请变更/ }),
    );
    await user.click(screen.getByRole("button", { name: "提交申请" }));

    expect(await screen.findByText("请选择变更目标")).toBeInTheDocument();
    expect(screen.getByText("请选择新截止时间")).toBeInTheDocument();
    expect(screen.getByText("请输入变更原因")).toBeInTheDocument();
    expect(mockApi.post).not.toHaveBeenCalled();
  });

  it("提交主任务 DDL 变更，调用 POST /work-items/{id}/deadline-change-requests", async () => {
    const user = userEvent.setup();
    signInAs(assignee);
    stubGet({
      "/work-items/wi-1/deadline-change-requests": [],
      "/work-items/wi-1/collaboration-requests": [],
    });
    mockApi.post.mockResolvedValue({});

    renderWithProviders(<DeadlineChangeSection workItem={workItem} />);
    await user.click(
      await screen.findByRole("button", { name: /申请变更/ }),
    );

    await user.click(screen.getByRole("combobox"));
    await user.click(
      await screen.findByRole("option", { name: "主任务 DDL（RAG 检索管道）" }),
    );
    await user.type(screen.getByLabelText("新截止时间"), "2026-08-10T18:00");
    await user.type(
      screen.getByLabelText("变更原因"),
      "依赖方数据延期，需要顺延一周",
    );
    await user.click(screen.getByRole("button", { name: "提交申请" }));

    await waitFor(() => {
      expect(mockApi.post).toHaveBeenCalledWith(
        "/work-items/wi-1/deadline-change-requests",
        expect.objectContaining({
          target_type: "work_item",
          target_id: "wi-1",
          new_due_at: new Date("2026-08-10T18:00").toISOString(),
          reason: "依赖方数据延期，需要顺延一周",
        }),
        expect.any(String),
      );
    });
    await waitFor(() => {
      expect(toast.success).toHaveBeenCalledWith("DDL 变更申请已提交");
    });
  });
});
