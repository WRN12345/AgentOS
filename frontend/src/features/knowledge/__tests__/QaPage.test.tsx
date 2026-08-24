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

import QaPage from "../QaPage";
import { mockApi } from "../../../test/mock-api";
import { renderWithProviders, signInAs } from "../../../test/render";
import { makeMember } from "../../../test/fixtures";
import type { QaResponse } from "../../../types";

const answered: QaResponse = {
  status: "answered",
  answer: "发布前先构建镜像 [1]。",
  sources: [
    {
      source_type: "document",
      source_id: "file-1",
      title: "部署指南.md",
      snippet: "发布步骤：先构建镜像",
    },
  ],
  clues: [],
};

const refused: QaResponse = {
  status: "refused",
  answer: null,
  sources: [],
  clues: [
    {
      source_type: "history",
      source_id: "wi-1",
      title: "工作项：支付接口改造",
      snippet: "毫不相干的记录",
    },
  ],
};

describe("QaPage（M7.4）", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    signInAs(makeMember());
  });

  it("完成一轮问答：提交问题后展示答案与依据数徽标", async () => {
    mockApi.post.mockResolvedValue(answered);
    renderWithProviders(<QaPage />);

    await userEvent.setup().type(screen.getByLabelText("问题"), "怎么部署");
    await userEvent.setup().click(screen.getByRole("button", { name: "提问" }));

    await waitFor(() =>
      expect(screen.getByText("发布前先构建镜像 [1]。")).toBeInTheDocument(),
    );
    expect(screen.getByText("依据 1 条")).toBeInTheDocument();
    expect(mockApi.post).toHaveBeenCalledWith("/memory/qa", {
      question: "怎么部署",
    });
  });

  it("拒答态：明确告知未找到并列出最接近的线索（16.13）", async () => {
    mockApi.post.mockResolvedValue(refused);
    renderWithProviders(<QaPage />);

    await userEvent.setup().type(screen.getByLabelText("问题"), "部署流程");
    await userEvent.setup().click(screen.getByRole("button", { name: "提问" }));

    await waitFor(() =>
      expect(screen.getByText("未找到相关内容")).toBeInTheDocument(),
    );
    expect(
      screen.getByText(/知识库里没有找到相关内容/),
    ).toBeInTheDocument();
    expect(screen.getByText("工作项：支付接口改造")).toBeInTheDocument();
  });

  it("接口失败时 toast 提示", async () => {
    mockApi.post.mockRejectedValue(new Error("网络错误"));
    const { toast } = await import("sonner");
    renderWithProviders(<QaPage />);

    await userEvent.setup().type(screen.getByLabelText("问题"), "x");
    await userEvent.setup().click(screen.getByRole("button", { name: "提问" }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("提问失败，请稍后重试"),
    );
  });
});
