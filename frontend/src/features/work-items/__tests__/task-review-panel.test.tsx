import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
vi.mock("../../../services/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../services/api")>();
  const { mockApi } = await import("../../../test/mock-api");
  return { ...actual, api: mockApi };
});
import {
  TaskReviewPanel,
  ReviewOpinion,
  DeliverableReviewPanel,
} from "../TaskReviewPanel";
import { DevDocReviewPanel, DevDocSection } from "../DevDocSection";
import { mockApi } from "../../../test/mock-api";
import { renderWithProviders, signInAs } from "../../../test/render";
import { makeMember, makeWorkItem } from "../../../test/fixtures";
import type {
  AgentRun,
  AgentSuggestion,
  Deliverable,
  DevDoc,
  ReviewContext,
} from "../../../types";

const doc: DevDoc = {
  id: "doc-1",
  work_item_id: "wi-1",
  work_item_title: "任务",
  author: null,
  content: "设计方案",
  status: "SUBMITTED",
  review_note: null,
  confirmed_by: null,
  confirmed_at: null,
  doc_version: 2,
  waived: false,
  latest_review_suggestion_id: null,
  version: 5,
  created_at: "2026-09-18T00:00:00Z",
  updated_at: "2026-09-18T00:00:00Z",
};
const deliverable: Deliverable = {
  id: "delivery-1",
  work_item_id: "wi-1",
  type: "text",
  content: "交付",
  file: null,
  version: 2,
  submitted_by: { id: "member-1", display_name: "成员" },
  created_at: doc.created_at,
  updated_at: doc.updated_at,
};
const context: ReviewContext = {
  dev_doc: {
    id: doc.id,
    doc_version: doc.doc_version,
    version: doc.version,
    content: doc.content,
  },
  deliverable: null,
  core_memory: [{ id: "memory-1", content: "接口使用统一错误码" }],
  core_memory_loaded: true,
};
function suggestion(overrides: Partial<AgentSuggestion> = {}): AgentSuggestion {
  return {
    id: "suggestion-1",
    run_id: "run-1",
    suggestion_type: "dev_doc_review",
    content: { summary: "需要补充边界条件", review_context: context },
    confidence: null,
    risks: null,
    fact_refs: null,
    review_status: "pending",
    reviewed_by: null,
    reviewed_at: null,
    prompt_version: null,
    work_item_id: "wi-1",
    model: null,
    created_at: doc.created_at,
    ...overrides,
  };
}
function run(overrides: Partial<AgentRun> = {}): AgentRun {
  return {
    id: "run-1",
    agent_type: "dev_doc_review",
    status: "failed",
    model: null,
    trigger_source: "auto",
    work_item_id: "wi-1",
    request_id: null,
    created_at: doc.created_at,
    error: "模型服务暂不可用",
    duration_ms: null,
    retry_count: 0,
    ...overrides,
  };
}

describe("任务内 AI 初审", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    mockApi.get.mockResolvedValue([]);
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it.each(["DRAFT", "SUBMITTED", "RETURNED"] as const)(
    "交付初审将 %s 文档视为未提供文档，与服务端快照匹配",
    async (status) => {
      mockApi.get.mockImplementation(async (url: string) => {
        if (url.endsWith("/deliverables")) return [deliverable];
        if (url.endsWith("/dev-doc")) return { ...doc, status };
        if (url.startsWith("/agent-runs")) return [];
        return [
          suggestion({
            suggestion_type: "review",
            content: {
              review_context: {
                ...context,
                dev_doc: null,
                deliverable: {
                  id: deliverable.id,
                  version: deliverable.version,
                },
              },
            },
          }),
        ];
      });
      renderWithProviders(<DeliverableReviewPanel workItemId="wi-1" />);
      expect(await screen.findByText("当前材料初审意见")).toBeInTheDocument();
      expect(screen.queryByText("历史初审意见")).not.toBeInTheDocument();
    },
  );

  it("交付文档读取失败保留未知状态，不能当作没有文档", async () => {
    mockApi.get.mockImplementation(async (url: string) => {
      if (url.endsWith("/deliverables")) return [deliverable];
      if (url.endsWith("/dev-doc")) throw new Error("unavailable");
      if (url.startsWith("/agent-runs")) return [];
      return [
        suggestion({
          suggestion_type: "review",
          content: {
            review_context: {
              ...context,
              dev_doc: null,
              deliverable: { id: deliverable.id, version: deliverable.version },
            },
          },
        }),
      ];
    });
    renderWithProviders(<DeliverableReviewPanel workItemId="wi-1" />);
    expect(
      await screen.findByText("初审意见（版本待确认）"),
    ).toBeInTheDocument();
    expect(screen.queryByText("当前材料初审意见")).not.toBeInTheDocument();
  });

  it("按任务和类型查询，展示最新相关运行与意见并隔离其他任务", async () => {
    mockApi.get.mockImplementation(async (url: string) =>
      url.startsWith("/agent-runs")
        ? [run({ work_item_id: "other", error: "其他任务错误" }), run()]
        : [
            suggestion({
              work_item_id: "other",
              content: { summary: "其他任务意见" },
            }),
            suggestion(),
          ],
    );
    renderWithProviders(
      <TaskReviewPanel
        workItemId="wi-1"
        agentType="dev_doc_review"
        devDoc={doc}
      />,
    );
    expect(await screen.findByText("需要补充边界条件")).toBeInTheDocument();
    expect(screen.getByText("当前材料初审意见")).toBeInTheDocument();
    expect(screen.queryByText("其他任务意见")).not.toBeInTheDocument();
    expect(mockApi.get).toHaveBeenCalledWith(
      "/agent-runs?work_item_id=wi-1&agent_type=dev_doc_review&limit=1",
    );
    expect(mockApi.get).toHaveBeenCalledWith(
      "/agent-suggestions?work_item_id=wi-1&suggestion_type=dev_doc_review&limit=1",
    );
    expect(screen.getByText("接口使用统一错误码")).toBeInTheDocument();
  });

  it("验收标准更新后将原初审标记为历史意见", () => {
    const item = makeWorkItem({ id: "wi-1", acceptance_criteria: "原验收标准" });
    renderWithProviders(
      <ReviewOpinion
        suggestion={suggestion({ content: { review_context: { ...context, work_item: item } } })}
        workItem={{ ...item, acceptance_criteria: "新增权限校验", version: item.version + 1 }}
        devDoc={doc}
      />,
    );
    expect(screen.getByText("历史初审意见")).toBeInTheDocument();
    expect(screen.getByText("验收标准：原验收标准")).toBeInTheDocument();
  });

  it("任务状态变化但要求与材料相同仍匹配", () => {
    const item = makeWorkItem({ id: "wi-1", status: "READY" });
    renderWithProviders(
      <ReviewOpinion
        suggestion={suggestion({ content: { review_context: { ...context, work_item: item } } })}
        workItem={{ ...item, status: "IN_PROGRESS", version: item.version + 1 }}
        devDoc={doc}
      />,
    );
    expect(screen.getByText("当前材料初审意见")).toBeInTheDocument();
  });

  it("失败运行可重试并刷新为等待状态", async () => {
    const user = userEvent.setup();
    let status: AgentRun["status"] = "failed";
    mockApi.get.mockImplementation(async (url: string) =>
      url.startsWith("/agent-runs") ? [run({ status })] : [],
    );
    mockApi.post.mockImplementation(async () => {
      status = "pending";
      return run({ status });
    });
    renderWithProviders(
      <TaskReviewPanel
        workItemId="wi-1"
        agentType="dev_doc_review"
        devDoc={doc}
      />,
    );
    await user.click(await screen.findByRole("button", { name: "重试初审" }));
    expect(mockApi.post).toHaveBeenCalledWith(
      "/agent-runs/run-1/retry",
      {},
      expect.any(String),
    );
    expect(await screen.findByText("等待初审")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "重试初审" }),
    ).not.toBeInTheDocument();
  });

  it.each(["pending", "running", "succeeded"] as const)(
    "%s 运行不提供重试",
    async (status) => {
      mockApi.get.mockImplementation(async (url: string) =>
        url.startsWith("/agent-runs") ? [run({ status })] : [],
      );
      renderWithProviders(
        <TaskReviewPanel
          workItemId="wi-1"
          agentType="dev_doc_review"
          devDoc={doc}
        />,
      );
      await screen.findByText(/最近运行：/);
      expect(
        screen.queryByRole("button", { name: "重试初审" }),
      ).not.toBeInTheDocument();
    },
  );

  it("重试错误保留意见并允许正式操作继续", async () => {
    mockApi.get.mockImplementation(async (url: string) =>
      url.startsWith("/agent-runs") ? [run()] : [suggestion()],
    );
    mockApi.post.mockRejectedValue(new Error("network"));
    renderWithProviders(
      <TaskReviewPanel
        workItemId="wi-1"
        agentType="dev_doc_review"
        devDoc={doc}
      />,
    );
    await userEvent.click(
      await screen.findByRole("button", { name: "重试初审" }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent("初审重试失败");
    expect(screen.getByText("需要补充边界条件")).toBeInTheDocument();
    expect(screen.getByText(/AI 状态不影响提交与审核操作/)).toBeInTheDocument();
  });

  it("初始无运行时继续轮询以捕获自动触发与完成意见", async () => {
    vi.useFakeTimers();
    let started = false;
    mockApi.get.mockImplementation(async (url: string) =>
      !started
        ? []
        : url.startsWith("/agent-runs")
          ? [run({ status: "succeeded" })]
          : [suggestion()],
    );
    renderWithProviders(
      <TaskReviewPanel
        workItemId="wi-1"
        agentType="dev_doc_review"
        devDoc={doc}
      />,
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50);
    });
    expect(
      screen.getByText("暂无初审运行记录，提交后自动触发。"),
    ).toBeInTheDocument();
    started = true;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5100);
    });
    expect(screen.getByText("初审运行完成")).toBeInTheDocument();
    expect(screen.getByText("需要补充边界条件")).toBeInTheDocument();
  });

  it("文档内容或提交版本变化明确标注历史意见", () => {
    renderWithProviders(
      <ReviewOpinion
        suggestion={suggestion()}
        devDoc={{ ...doc, content: "新方案" }}
      />,
    );
    expect(screen.getByText("历史初审意见")).toBeInTheDocument();
    expect(screen.queryByText("当前材料初审意见")).not.toBeInTheDocument();
  });

  it("确认造成记录版本变化但材料相同仍匹配", () => {
    renderWithProviders(
      <ReviewOpinion
        suggestion={suggestion()}
        devDoc={{ ...doc, version: 6, status: "CONFIRMED" }}
      />,
    );
    expect(screen.getByText("当前材料初审意见")).toBeInTheDocument();
  });

  it("旧建议缺少快照明确无法确认版本", () => {
    renderWithProviders(
      <ReviewOpinion
        suggestion={suggestion({ content: { summary: "旧意见" } })}
        devDoc={doc}
      />,
    );
    expect(
      screen.getByText("该意见未记录材料快照，无法确认对应版本。"),
    ).toBeInTheDocument();
    expect(screen.queryByText("当前材料初审意见")).not.toBeInTheDocument();
  });

  it("交付物版本变化标记历史并正确查询 review 建议", async () => {
    mockApi.get.mockImplementation(async (url: string) =>
      url.startsWith("/agent-runs")
        ? []
        : [
            suggestion({
              suggestion_type: "review",
              content: {
                review_context: {
                  ...context,
                  deliverable: { id: "old", version: 1 },
                },
              },
            }),
          ],
    );
    renderWithProviders(
      <TaskReviewPanel
        workItemId="wi-1"
        agentType="deliverable_review"
        devDoc={doc}
        deliverable={deliverable}
      />,
    );
    expect(await screen.findByText("历史初审意见")).toBeInTheDocument();
    expect(mockApi.get).toHaveBeenCalledWith(
      "/agent-runs?work_item_id=wi-1&agent_type=deliverable_review&limit=1",
    );
    expect(mockApi.get).toHaveBeenCalledWith(
      "/agent-suggestions?work_item_id=wi-1&suggestion_type=review&limit=1",
    );
  });

  it("项目约定加载失败明确提示", () => {
    renderWithProviders(
      <ReviewOpinion
        suggestion={suggestion({
          content: {
            review_context: {
              ...context,
              core_memory: [],
              core_memory_loaded: false,
            },
          },
        })}
        devDoc={doc}
      />,
    );
    expect(
      screen.getByText("项目约定加载失败，本次初审未获得完整约定。"),
    ).toBeInTheDocument();
  });

  it("外部审批面板使用任务定向查询并保留指定意见", async () => {
    mockApi.get.mockResolvedValue([suggestion()]);
    renderWithProviders(
      <DevDocReviewPanel suggestionId="suggestion-1" workItemId="wi-1" />,
    );
    expect(await screen.findByText("需要补充边界条件")).toBeInTheDocument();
    expect(mockApi.get).toHaveBeenCalledWith(
      "/agent-suggestions?work_item_id=wi-1&suggestion_type=dev_doc_review",
    );
  });

  it("初审查询失败仍可正式确认开发文档", async () => {
    signInAs(makeMember({ role: "leader" }));
    mockApi.get.mockImplementation(async (url: string) => {
      if (url.endsWith("/dev-doc")) return doc;
      throw new Error("AI unavailable");
    });
    mockApi.post.mockResolvedValue({ ...doc, status: "CONFIRMED" });
    renderWithProviders(
      <DevDocSection workItem={makeWorkItem({ id: "wi-1" })} />,
    );
    await userEvent.click(
      await screen.findByRole("button", { name: "确认通过" }),
    );
    await waitFor(() =>
      expect(mockApi.post).toHaveBeenCalledWith(
        "/work-items/wi-1/dev-doc/confirm",
        { version: 5 },
        expect.any(String),
      ),
    );
  });
});
