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
import CoreMemoryPage from "../CoreMemoryPage";
import { mockApi, stubGet } from "../../../test/mock-api";
import { renderWithProviders, signInAs } from "../../../test/render";
import { makeLeader, makeMember, memberBrief } from "../../../test/fixtures";
import type { CoreMemoryEntryList } from "../../../types";

const entryList: CoreMemoryEntryList = {
  entries: [
    {
      id: "cm-1",
      scope: "project",
      content: "本项目禁用递归查询",
      status: "active",
      proposed_by: null,
      confirmed_by: memberBrief({ id: "member-leader", display_name: "负责人" }),
      effective_at: "2026-08-20T08:00:00Z",
      created_at: "2026-08-20T08:00:00Z",
    },
    {
      id: "cm-2",
      scope: "project",
      content: "旧约定（已过时）",
      status: "deprecated",
      proposed_by: memberBrief({ id: "member-leader", display_name: "负责人" }),
      confirmed_by: memberBrief({ id: "member-leader", display_name: "负责人" }),
      effective_at: "2026-08-01T08:00:00Z",
      created_at: "2026-08-01T08:00:00Z",
    },
  ],
  used_chars: 9,
  budget_chars: 4000,
};

describe("CoreMemoryPage（M4.7）", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    stubGet({ "/memory/core-entries": entryList });
  });

  it("成员可读条目列表：来源信息与容量占用可见，无写入口", async () => {
    signInAs(makeMember());
    renderWithProviders(<CoreMemoryPage />);

    await waitFor(() =>
      expect(screen.getByText("本项目禁用递归查询")).toBeInTheDocument(),
    );
    // 来源信息：Agent 提议 / 确认者 / 状态
    expect(screen.getByText("AI 提议")).toBeInTheDocument();
    expect(screen.getAllByText("负责人").length).toBeGreaterThan(0);
    expect(screen.getByText("生效中")).toBeInTheDocument();
    expect(screen.getByText("已作废")).toBeInTheDocument();
    // 容量占用
    expect(screen.getByText("已用 9 / 4000 字符")).toBeInTheDocument();
    // 非负责人无写入口
    expect(screen.queryByText("手写条目")).not.toBeInTheDocument();
    expect(screen.queryByText("作废")).not.toBeInTheDocument();
  });

  it("负责人可手写条目：提交后生效并刷新列表", async () => {
    signInAs(makeLeader());
    mockApi.post.mockResolvedValue({});
    renderWithProviders(<CoreMemoryPage />);

    await waitFor(() => expect(screen.getByText("手写条目")).toBeInTheDocument());
    await userEvent.setup().type(
      screen.getByLabelText("内容"),
      "支付模块走独立服务",
    );
    await userEvent.setup().click(screen.getByText("添加并生效"));

    await waitFor(() =>
      expect(mockApi.post).toHaveBeenCalledWith("/memory/core-entries", {
        content: "支付模块走独立服务",
      }),
    );
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith("核心记忆已添加并生效"),
    );
  });

  it("负责人可作废生效条目；已作废条目无作废按钮", async () => {
    signInAs(makeLeader());
    mockApi.post.mockResolvedValue({});
    renderWithProviders(<CoreMemoryPage />);

    await waitFor(() =>
      expect(screen.getByText("本项目禁用递归查询")).toBeInTheDocument(),
    );
    const deprecateButtons = screen.getAllByText("作废");
    expect(deprecateButtons).toHaveLength(1); // 仅生效条目可作废
    await userEvent.setup().click(deprecateButtons[0]);

    await waitFor(() =>
      expect(mockApi.post).toHaveBeenCalledWith(
        "/memory/core-entries/cm-1/deprecate",
      ),
    );
  });

  it("空列表时如实标注积累尚少（16.11）", async () => {
    stubGet({
      "/memory/core-entries": { entries: [], used_chars: 0, budget_chars: 4000 },
    });
    signInAs(makeMember());
    renderWithProviders(<CoreMemoryPage />);

    await waitFor(() =>
      expect(
        screen.getByText("暂无核心记忆——本项目积累尚少"),
      ).toBeInTheDocument(),
    );
  });
});
