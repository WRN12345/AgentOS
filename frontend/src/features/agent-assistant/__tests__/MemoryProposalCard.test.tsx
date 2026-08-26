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
import AgentAssistantPage from "../AgentAssistantPage";
import { mockApi, stubGet } from "../../../test/mock-api";
import { renderWithProviders, signInAs } from "../../../test/render";
import { makeLeader } from "../../../test/fixtures";
import type { AgentSuggestion } from "../../../types";

function makeProposal(overrides: Partial<AgentSuggestion> = {}): AgentSuggestion {
  return {
    id: "sug-1",
    run_id: "run-1",
    suggestion_type: "memory_proposal",
    content: {
      action: "create",
      content: "改 X 表结构一定要同步改 Y",
      reason: "踩坑教训",
    },
    confidence: null,
    risks: null,
    fact_refs: null,
    review_status: "pending",
    reviewed_by: null,
    reviewed_at: null,
    prompt_version: null,
    work_item_id: null,
    model: "qwen3",
    created_at: "2026-08-22T08:00:00Z",
    ...overrides,
  };
}

describe("核心记忆提议确认入口（M4.8）", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    signInAs(makeLeader());
    mockApi.post.mockResolvedValue({});
  });

  it("待确认提议：负责人展开后可确认生效，确认后刷新核心记忆", async () => {
    stubGet({ "/agent-suggestions": [makeProposal()] });
    renderWithProviders(<AgentAssistantPage />);

    // 头部摘要回退为"动作 + 内容预览"
    await waitFor(() =>
      expect(
        screen.getByText("新增条目：改 X 表结构一定要同步改 Y"),
      ).toBeInTheDocument(),
    );
    expect(screen.getByText("核心记忆提议")).toBeInTheDocument();

    // 操作按钮在展开详情内
    await userEvent.setup().click(screen.getByText("展开详情"));
    expect(screen.getByText("理由")).toBeInTheDocument();
    await userEvent.setup().click(screen.getByText("确认生效"));

    await waitFor(() =>
      expect(mockApi.post).toHaveBeenCalledWith(
        "/agent-suggestions/sug-1/feedback",
        { action: "accepted" },
        expect.anything(),
      ),
    );
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith("已确认，核心记忆已生效"),
    );
  });

  it("待确认提议可拒绝", async () => {
    stubGet({ "/agent-suggestions": [makeProposal()] });
    renderWithProviders(<AgentAssistantPage />);

    await waitFor(() => expect(screen.getByText("展开详情")).toBeInTheDocument());
    await userEvent.setup().click(screen.getByText("展开详情"));
    await userEvent.setup().click(screen.getByText("拒绝"));

    await waitFor(() =>
      expect(mockApi.post).toHaveBeenCalledWith(
        "/agent-suggestions/sug-1/feedback",
        { action: "ignored" },
        expect.anything(),
      ),
    );
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith("已拒绝该提议"),
    );
  });

  it("过期提议展示已过期样式且不可再确认（16.6）", async () => {
    stubGet({
      "/agent-suggestions": [makeProposal({ review_status: "expired" })],
    });
    renderWithProviders(<AgentAssistantPage />);

    await waitFor(() => expect(screen.getByText("已过期")).toBeInTheDocument());
    await userEvent.setup().click(screen.getByText("展开详情"));
    expect(screen.queryByText("确认生效")).not.toBeInTheDocument();
    expect(screen.queryByText("拒绝")).not.toBeInTheDocument();
  });
});
