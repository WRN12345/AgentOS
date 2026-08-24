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


describe("QaPage 依据列表与原文查看（M7.5）", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    signInAs(makeMember());
  });

  it("答案下方列出依据，点击文档依据弹原文并可下载", async () => {
    mockApi.post.mockResolvedValue(answered);
    renderWithProviders(<QaPage />);

    await userEvent.setup().type(screen.getByLabelText("问题"), "怎么部署");
    await userEvent.setup().click(screen.getByRole("button", { name: "提问" }));
    await waitFor(() =>
      expect(screen.getByText("依据（点击查看原文）")).toBeInTheDocument(),
    );

    await userEvent.setup().click(screen.getByText("部署指南.md"));
    await waitFor(() =>
      expect(screen.getByText(/答案依据的原文片段/)).toBeInTheDocument(),
    );
    expect(screen.getAllByText("发布步骤：先构建镜像").length).toBeGreaterThan(0);
    await userEvent
      .setup()
      .click(screen.getByRole("button", { name: "下载原文" }));
    expect(mockApi.downloadFile).toHaveBeenCalledWith("/files/file-1/download");
  });

  it("历史记录依据提供关联工作项入口", async () => {
    mockApi.post.mockResolvedValue({
      ...answered,
      sources: [
        {
          source_type: "history",
          source_id: "wi-9",
          title: "工作项：支付接口改造",
          snippet: "工作项完成记录：支付接口改造",
        },
      ],
    });
    renderWithProviders(<QaPage />);

    await userEvent.setup().type(screen.getByLabelText("问题"), "支付");
    await userEvent.setup().click(screen.getByRole("button", { name: "提问" }));
    await waitFor(() =>
      expect(screen.getByText("工作项：支付接口改造")).toBeInTheDocument(),
    );

    await userEvent.setup().click(screen.getByText("工作项：支付接口改造"));
    await waitFor(() =>
      expect(
        screen.getByRole("link", { name: "查看关联工作项" }),
      ).toHaveAttribute("href", "/work-items/wi-9"),
    );
  });
});


describe("QaPage 冷启动标注（M7.6，16.11）", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    signInAs(makeMember());
  });

  it("检索结果稀少时标注「本项目积累尚少」", async () => {
    mockApi.post.mockResolvedValue(answered); // 仅 1 条依据
    renderWithProviders(<QaPage />);

    await userEvent.setup().type(screen.getByLabelText("问题"), "怎么部署");
    await userEvent.setup().click(screen.getByRole("button", { name: "提问" }));

    await waitFor(() =>
      expect(screen.getByText("本项目积累尚少")).toBeInTheDocument(),
    );
  });

  it("积累充足（多条依据）时不出现标注", async () => {
    mockApi.post.mockResolvedValue({
      ...answered,
      sources: [
        ...answered.sources,
        {
          source_type: "history",
          source_id: "wi-2",
          title: "工作项：部署流水线",
          snippet: "工作项完成记录",
        },
      ],
    });
    renderWithProviders(<QaPage />);

    await userEvent.setup().type(screen.getByLabelText("问题"), "怎么部署");
    await userEvent.setup().click(screen.getByRole("button", { name: "提问" }));

    await waitFor(() =>
      expect(screen.getByText("依据 2 条")).toBeInTheDocument(),
    );
    expect(screen.queryByText("本项目积累尚少")).not.toBeInTheDocument();
  });

  it("拒答且线索稀少时同样标注", async () => {
    mockApi.post.mockResolvedValue(refused); // 仅 1 条线索
    renderWithProviders(<QaPage />);

    await userEvent.setup().type(screen.getByLabelText("问题"), "部署流程");
    await userEvent.setup().click(screen.getByRole("button", { name: "提问" }));

    await waitFor(() =>
      expect(screen.getByText("本项目积累尚少")).toBeInTheDocument(),
    );
  });
});
