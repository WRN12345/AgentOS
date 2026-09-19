import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route, Routes } from "react-router-dom";
vi.mock("../../../services/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../services/api")>();
  const { mockApi } = await import("../../../test/mock-api");
  return { ...actual, api: mockApi };
});
vi.mock("../../../services/events", () => ({ useEventStream: vi.fn() }));
import { mockApi, stubGet } from "../../../test/mock-api";
import { renderWithProviders, signInAs } from "../../../test/render";
import { makeAdminProject, makeMember, makeUser } from "../../../test/fixtures";
import { queryKeys } from "../../../lib/queryKeys";
import { RequirementCard } from "../RequirementCard";
import ProjectRequirementsPage from "../ProjectRequirementsPage";
import ProjectRequirementsPanel from "../../admin/ProjectRequirementsPanel";
import type { Requirement } from "../types";
import AdminConsolePage from "../../admin/AdminConsolePage";
import AppLayout from "../../../components/AppLayout";

const base = "/admin/projects/project-a";
const requirement: Requirement = {
  id: "req-1",
  title: "合同需求",
  description: "需求描述",
  acceptance_criteria: "验收通过",
  clarification_questions: "",
  sources: [
    {
      material_id: "m1",
      filename: "contract.txt",
      chunk_id: "c1",
      quote: "合同原始条款",
    },
  ],
  status: "draft",
  version: 2,
  assignee_id: null,
  leader_note: null,
  discussion: [],
  created_at: "2026-09-18T00:00:00Z",
  updated_at: "2026-09-18T00:00:00Z",
};
const material = {
  id: "m1",
  original_filename: "contract.txt",
  size_bytes: 100,
  created_at: requirement.created_at,
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

describe("项目需求", () => {
  it("负责人从项目需求打开 AI 拆解向导", async () => {
    signInAs(makeMember({ role: "leader" }));
    stubGet({
      "/project-requirements": [],
      "/members": [],
      "/config": { llm_is_external: false },
    });
    renderWithProviders(<ProjectRequirementsPage />);
    await userEvent.setup().click(screen.getByRole("button", { name: "AI 需求拆解" }));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    await waitFor(() => expect(mockApi.get).toHaveBeenCalledWith("/members"));
  });

  it.each(["draft", "confirmed", "dispatched", "clarification_requested"] as const)(
    "admin replies to assigned %s with exact versioned payload", async (status) => {
      const assigned = { ...requirement, status, assignee_id: "leader-1" };
      mockApi.post.mockResolvedValue({ ...assigned, status: "dispatched", version: 3 });
      renderWithProviders(<RequirementCard requirement={assigned} adminPath={`${base}/requirements`} onChanged={vi.fn()} />);
      const button = screen.getByRole("button", { name: status === "draft" ? "确认并回复负责人" : "答复并发送负责人" });
      expect(button).toBeDisabled();
      fireEvent.change(screen.getByLabelText("回复说明"), { target: { value: "  已明确范围  " } });
      fireEvent.click(button);
      await waitFor(() => expect(mockApi.post).toHaveBeenCalledWith(
        `${base}/requirements/req-1/reply`, { version: 2, note: "已明确范围" }, expect.any(String),
      ));
      expect(mockApi.patch).not.toHaveBeenCalled();
    },
  );

  it("atomically revises and replies, requiring resolved content and explicit reply", async () => {
    renderWithProviders(<RequirementCard requirement={{ ...requirement, assignee_id: "leader-1", status: "clarification_requested" }} adminPath={`${base}/requirements`} onChanged={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "编辑需求" }));
    const save = screen.getByRole("button", { name: "保存并回复负责人" });
    expect(screen.queryByRole("button", { name: "保存草稿" })).not.toBeInTheDocument();
    expect(save).toBeDisabled();
    fireEvent.change(screen.getByLabelText("回复说明"), { target: { value: "已修订" } });
    for (const label of ["标题", "描述", "验收标准"]) {
      const input = screen.getByLabelText(label);
      const original = (input as HTMLInputElement).value;
      fireEvent.change(input, { target: { value: " " } });
      expect(save).toBeDisabled();
      fireEvent.change(input, { target: { value: original } });
    }
    fireEvent.change(screen.getByLabelText("澄清问题"), { target: { value: "未解决" } });
    expect(save).toBeDisabled();
    fireEvent.change(screen.getByLabelText("澄清问题"), { target: { value: "" } });
    fireEvent.change(screen.getByLabelText("标题"), { target: { value: "修订标题" } });
    mockApi.post.mockRejectedValue(new Error("conflict"));
    fireEvent.click(save);
    await screen.findByRole("alert");
    expect(mockApi.post).toHaveBeenCalledWith(`${base}/requirements/req-1/reply`, {
      version: 2, note: "已修订", content: { title: "修订标题", description: "需求描述", acceptance_criteria: "验收通过", clarification_questions: "" },
    }, expect.any(String));
    expect(mockApi.patch).not.toHaveBeenCalled();
    expect(screen.getByLabelText("标题")).toHaveValue("修订标题");
    expect(screen.getByLabelText("回复说明")).toHaveValue("已修订");
  });

  it.each(["accepted", "excluded", "unassigned"])("does not offer admin replies for %s", (state) => {
    renderWithProviders(<RequirementCard requirement={{ ...requirement, assignee_id: state === "unassigned" ? null : "leader-1", status: state === "unassigned" ? "draft" : state as "accepted" | "excluded" }} adminPath={`${base}/requirements`} onChanged={vi.fn()} />);
    expect(screen.queryByLabelText("回复说明")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /回复负责人|答复并发送负责人/ })).not.toBeInTheDocument();
  });

  it("preserves assigned dirty edits and blocks atomic reply after a newer version arrives", () => {
    const assigned: Requirement = { ...requirement, assignee_id: "leader-1", status: "clarification_requested" };
    const view = renderWithProviders(<RequirementCard requirement={assigned} adminPath={`${base}/requirements`} onChanged={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "编辑需求" }));
    fireEvent.change(screen.getByLabelText("标题"), { target: { value: "本地修改" } });
    fireEvent.change(screen.getByLabelText("回复说明"), { target: { value: "已修订" } });
    view.rerender(<RequirementCard requirement={{ ...assigned, version: 3 }} adminPath={`${base}/requirements`} onChanged={vi.fn()} />);
    expect(screen.getByLabelText("标题")).toHaveValue("本地修改");
    expect(screen.getByLabelText("回复说明")).toHaveValue("已修订");
    expect(screen.getByRole("button", { name: "保存并回复负责人" })).toBeDisabled();
    expect(screen.getByRole("alert")).toHaveTextContent("需求已有新版本");
  });

  it.each([true, false])("shows chronological discussion for admin=%s with migrated feedback", (admin) => {
    renderWithProviders(<RequirementCard requirement={{ ...requirement, leader_note: "历史问题", discussion: [
      { id: "2", author_id: "admin", author_role: "admin", body: "管理员回复", created_at: requirement.created_at, version: 3 },
      { id: "1", author_id: null, author_role: "leader", body: "历史问题", created_at: null, version: null },
    ] }} adminPath={admin ? `${base}/requirements` : undefined} onChanged={vi.fn()} />);
    expect(screen.getAllByText("历史问题")).toHaveLength(1);
    const entries = screen.getAllByRole("listitem");
    expect(entries[0]).toHaveTextContent("项目负责人 · 历史反馈历史问题");
    expect(entries[1]).toHaveTextContent("管理员");
    expect(entries[1].querySelector("time")).toHaveAttribute("datetime", requirement.created_at);
  });

  it("allows leader followup while waiting and locks accepted requirements", async () => {
    const waiting: Requirement = { ...requirement, status: "clarification_requested" };
    const view = renderWithProviders(<RequirementCard requirement={waiting} onChanged={vi.fn()} />);
    expect(screen.getByText("等待管理员回复")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "接收需求" })).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("澄清说明"), { target: { value: "补充问题" } });
    mockApi.post.mockResolvedValue({ ...waiting, version: 3 });
    fireEvent.click(screen.getByRole("button", { name: "继续追问" }));
    await waitFor(() => expect(mockApi.post).toHaveBeenCalledWith("/project-requirements/req-1/clarify", { version: 2, note: "补充问题" }, expect.any(String)));
    view.rerender(<RequirementCard requirement={{ ...waiting, status: "accepted" }} onChanged={vi.fn()} />);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it.each(["admin", "leader"])("polls %s requirements visibly every five seconds, pauses on error and retries manually", async (role) => {
    vi.useFakeTimers();
    const path = role === "admin" ? `${base}/requirements` : "/project-requirements";
    if (role === "leader") signInAs(makeMember({ role: "leader" }));
    stubGet({ [path]: [requirement] });
    const view = renderWithProviders(role === "admin" ? <ProjectRequirementsPanel projectId="project-a" /> : <ProjectRequirementsPage />);
    const count = () => mockApi.get.mock.calls.filter(([url]) => url === path).length;
    await act(async () => { await vi.advanceTimersByTimeAsync(10); });
    expect(count()).toBe(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(count()).toBe(2);
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" });
    fireEvent(document, new Event("visibilitychange"));
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(count()).toBe(2);
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
    mockApi.get.mockRejectedValue(new Error("offline"));
    fireEvent(document, new Event("visibilitychange"));
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    const failedCount = count();
    await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
    expect(count()).toBe(failedCount);
    stubGet({ [path]: [requirement] });
    fireEvent.click(screen.getByRole("button", { name: "刷新需求" }));
    await act(async () => { await vi.advanceTimersByTimeAsync(5010); });
    expect(count()).toBeGreaterThan(failedCount + 1);
    view.unmount();
  });

  beforeEach(() => {
    vi.resetAllMocks();
    signInAs(null, makeUser({ is_admin: true }));
    stubGet({});
  });
  afterEach(() => vi.useRealTimers());

  it.each(["overview", "projects"])(
    "mounts requirements only in projects-tab details, not %s supervision",
    async (tab) => {
      stubGet({
        "/admin/overview": {
          as_of: requirement.created_at,
          projects: [
            {
              ...makeAdminProject({ id: "project-a" }),
              total: 0,
              completed: 0,
              active: 0,
              overdue: 0,
              blocked: 0,
            },
          ],
          members: [],
        },
        "/admin/attention": { items: [], total: 0 },
      });
      renderWithProviders(<AdminConsolePage />, {
        route: `/console?tab=${tab}&project=project-a`,
      });
      await screen.findByText("成员工作量");
      if (tab === "projects") {
        expect(
          screen.getByRole("region", { name: "项目需求管理" }),
        ).toBeInTheDocument();
        expect(screen.getByText(/任务监督 · 只读/)).toBeInTheDocument();
        expect(mockApi.get).toHaveBeenCalledWith(`${base}/requirements`);
      } else {
        expect(
          screen.queryByRole("region", { name: "项目需求管理" }),
        ).not.toBeInTheDocument();
        expect(mockApi.get).not.toHaveBeenCalledWith(`${base}/requirements`);
      }
    },
  );

  it.each(["leader", "member"] as const)(
    "guards requirements navigation for %s",
    async (role) => {
      signInAs(makeMember({ role }));
      renderWithProviders(<AppLayout />);
      await act(async () => {});
      if (role === "leader")
        expect(screen.getByRole("link", { name: "项目需求" })).toHaveAttribute(
          "href",
          "/project-requirements",
        );
      else
        expect(
          screen.queryByRole("link", { name: "项目需求" }),
        ).not.toBeInTheDocument();
    },
  );

  it("retries a failed materials query without a misleading empty state", async () => {
    mockApi.get.mockImplementation((path: string) =>
      path.endsWith("materials")
        ? Promise.reject(new Error("offline"))
        : Promise.resolve([]),
    );
    renderWithProviders(<ProjectRequirementsPanel projectId="project-a" />);
    await screen.findByText("材料加载失败");
    expect(screen.queryByText("暂无材料")).not.toBeInTheDocument();
    stubGet({ [`${base}/materials`]: [material] });
    await userEvent
      .setup()
      .click(screen.getByRole("button", { name: "重试材料" }));
    expect(await screen.findByRole("checkbox")).toBeInTheDocument();
  });

  it("requires clarification revisions before confirmation and nonblank descriptions", async () => {
    const user = userEvent.setup();
    renderWithProviders(
      <RequirementCard
        requirement={{ ...requirement, status: "clarification_requested" }}
        adminPath={`${base}/requirements`}
        onChanged={vi.fn()}
      />,
    );
    expect(screen.queryByRole("button", { name: "确认需求" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "排除需求" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "编辑需求" }));
    await user.clear(screen.getByLabelText("描述"));
    await user.type(screen.getByLabelText("描述"), "   ");
    expect(screen.getByRole("button", { name: "保存草稿" })).toBeDisabled();
    expect(mockApi.post).not.toHaveBeenCalled();
    expect(mockApi.patch).not.toHaveBeenCalled();
  });

  it("retains editable values on a failed save", async () => {
    const user = userEvent.setup();
    mockApi.patch.mockRejectedValue(new Error("offline"));
    renderWithProviders(
      <RequirementCard
        requirement={requirement}
        adminPath={`${base}/requirements`}
        onChanged={vi.fn()}
      />,
    );
    await user.click(screen.getByRole("button", { name: "编辑需求" }));
    await user.type(screen.getByLabelText("标题"), "修订");
    await user.click(screen.getByRole("button", { name: "保存草稿" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("操作失败");
    expect(screen.getByLabelText("标题")).toHaveValue("合同需求修订");
  });

  it("applies a saved draft immediately and preserves subsequent edits across delayed revalidation", async () => {
    const user = userEvent.setup();
    const saved = { ...requirement, title: "已保存标题", version: 3 };
    const refresh = deferred<Requirement[]>();
    stubGet({
      [`${base}/requirements`]: [{ ...requirement, status: "confirmed" }],
    });
    const view = renderWithProviders(
      <ProjectRequirementsPanel projectId="project-a" />,
    );
    await user.click(await screen.findByRole("button", { name: "编辑需求" }));
    await user.clear(screen.getByLabelText("标题"));
    await user.type(screen.getByLabelText("标题"), saved.title);
    mockApi.get.mockImplementation((path: string) =>
      path === `${base}/requirements` ? refresh.promise : Promise.resolve([]),
    );
    mockApi.get.mockClear();
    mockApi.patch.mockResolvedValueOnce(saved);
    await user.click(screen.getByRole("button", { name: "保存草稿" }));
    await screen.findByText(saved.title);
    expect(
      view.queryClient.getQueryData(
        queryKeys.adminRequirements("project-a", "requirements"),
      ),
    ).toEqual([saved]);
    expect(
      screen.queryByRole("button", { name: "派发给负责人" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "确认需求" })).toBeEnabled();
    expect(mockApi.get.mock.calls.map(([path]) => path)).toEqual([
      `${base}/requirements`,
    ]);
    await user.click(screen.getByRole("button", { name: "编辑需求" }));
    await user.type(screen.getByLabelText("标题"), "继续编辑");
    await act(async () => {
      refresh.resolve([saved]);
    });
    await waitFor(() => expect(view.queryClient.isFetching()).toBe(0));
    expect(screen.getByLabelText("标题")).toHaveValue("已保存标题继续编辑");
    const nextSave = deferred<Requirement>();
    mockApi.patch.mockReturnValueOnce(nextSave.promise);
    await user.click(screen.getByRole("button", { name: "保存草稿" }));
    expect(mockApi.patch).toHaveBeenLastCalledWith(
      `${base}/requirements/req-1`,
      expect.objectContaining({ title: "已保存标题继续编辑", version: 3 }),
      expect.any(String),
    );
    expect(screen.getByLabelText("标题")).toBeDisabled();
    view.unmount();
    nextSave.resolve({ ...saved, version: 4 });
  });

  it.each(["confirmed", "dispatched"] as const)(
    "preserves dirty edits when a delayed refresh returns a newer %s version",
    async (status) => {
      const user = userEvent.setup();
      const latest = {
        ...requirement,
        title: "其他管理员更新",
        version: 3,
        status,
      };
      const refresh = deferred<Requirement[]>();
      stubGet({ [`${base}/requirements`]: [requirement] });
      const view = renderWithProviders(
        <ProjectRequirementsPanel projectId="project-a" />,
      );
      await user.click(await screen.findByRole("button", { name: "编辑需求" }));
      await user.type(screen.getByLabelText("标题"), "本地修改");
      mockApi.get.mockImplementation((path: string) =>
        path === `${base}/requirements` ? refresh.promise : Promise.resolve([]),
      );
      await user.click(screen.getByRole("button", { name: "刷新需求" }));
      await act(async () => {
        refresh.resolve([latest]);
      });
      await screen.findByText(/需求已有新版本/);
      expect(screen.getByLabelText("标题")).toHaveValue("合同需求本地修改");
      expect(screen.getByRole("button", { name: "保存草稿" })).toBeDisabled();
      expect(mockApi.patch).not.toHaveBeenCalled();
      if (status === "confirmed") {
        await user.click(
          screen.getByRole("button", { name: "放弃修改并加载最新版本" }),
        );
        expect(screen.getByLabelText("标题")).toHaveValue(latest.title);
        mockApi.patch.mockResolvedValue({
          ...latest,
          version: 4,
          status: "draft",
        });
        await user.click(screen.getByRole("button", { name: "保存草稿" }));
        expect(mockApi.patch).toHaveBeenCalledWith(
          `${base}/requirements/req-1`,
          expect.objectContaining({ version: 3, title: latest.title }),
          expect.any(String),
        );
      } else {
        expect(
          screen.queryByRole("button", { name: "放弃修改并加载最新版本" }),
        ).not.toBeInTheDocument();
        await user.click(screen.getByRole("button", { name: "取消" }));
        expect(
          screen.queryByRole("button", { name: "编辑需求" }),
        ).not.toBeInTheDocument();
      }
      view.unmount();
    },
  );

  it("cancels pre-mutation reads and exposes confirmed state before delayed revalidation", async () => {
    const user = userEvent.setup();
    const oldRead = deferred<Requirement[]>();
    const newRead = deferred<Requirement[]>();
    const confirmed = {
      ...requirement,
      version: 3,
      status: "confirmed" as const,
    };
    stubGet({ [`${base}/requirements`]: [requirement] });
    const view = renderWithProviders(
      <ProjectRequirementsPanel projectId="project-a" />,
    );
    await screen.findByText(requirement.title);
    let reads = 0;
    mockApi.get.mockImplementation((path: string) =>
      path === `${base}/requirements`
        ? ++reads === 1
          ? oldRead.promise
          : newRead.promise
        : Promise.resolve([]),
    );
    await user.click(screen.getByRole("button", { name: "刷新需求" }));
    mockApi.post.mockResolvedValueOnce(confirmed);
    await user.click(screen.getByRole("button", { name: "确认需求" }));
    await screen.findByRole("button", { name: "派发给负责人" });
    await act(async () => {
      oldRead.resolve([requirement]);
    });
    expect(
      view.queryClient.getQueryData(
        queryKeys.adminRequirements("project-a", "requirements"),
      ),
    ).toEqual([confirmed]);
    expect(
      screen.queryByRole("button", { name: "确认需求" }),
    ).not.toBeInTheDocument();
    await act(async () => {
      newRead.resolve([confirmed]);
    });
    await waitFor(() => expect(view.queryClient.isFetching()).toBe(0));
  });

  it.each(["accept", "clarify"] as const)(
    "applies leader %s response before a delayed list refresh",
    async (action) => {
      const user = userEvent.setup();
      signInAs(makeMember({ role: "leader" }));
      stubGet({
        "/project-requirements": [
          { ...requirement, status: "dispatched", sources: [] },
        ],
      });
      const view = renderWithProviders(<ProjectRequirementsPage />);
      await screen.findByText(requirement.title);
      const refresh = deferred<Requirement[]>();
      mockApi.get.mockReturnValue(refresh.promise);
      const updated: Requirement = {
        ...requirement,
        sources: [],
        version: 3,
        status: action === "accept" ? "accepted" : "clarification_requested",
        leader_note: action === "clarify" ? "待明确范围" : null,
      };
      mockApi.post.mockResolvedValue(updated);
      if (action === "clarify")
        await user.type(screen.getByLabelText("澄清说明"), "待明确范围");
      await user.click(
        screen.getByRole("button", {
          name: action === "accept" ? "接收需求" : "请求澄清",
        }),
      );
      await screen.findByText(action === "accept" ? "已接收" : "待澄清");
      expect(
        view.queryClient.getQueryData(queryKeys.projectRequirements()),
      ).toEqual([updated]);
      expect(
        screen.queryByRole("button", { name: "接收需求" }),
      ).not.toBeInTheDocument();
      expect(
        screen.queryByRole("button", { name: "请求澄清" }),
      ).not.toBeInTheDocument();
      await act(async () => {
        refresh.resolve([updated]);
      });
      await waitFor(() => expect(view.queryClient.isFetching()).toBe(0));
    },
  );

  it("uploads multipart materials and analyzes selected files at the explicit admin project path", async () => {
    const user = userEvent.setup();
    stubGet({ [`${base}/materials`]: [material] });
    mockApi.upload.mockResolvedValue(material);
    mockApi.post.mockResolvedValue({ id: "a1", status: "pending" });
    renderWithProviders(<ProjectRequirementsPanel projectId="project-a" />);
    const file = new File(["合同"], "contract.txt", { type: "text/plain" });
    await user.upload(screen.getByLabelText("上传需求材料"), file);
    await user.click(screen.getByRole("button", { name: "上传材料" }));
    await waitFor(() => expect(mockApi.upload).toHaveBeenCalled());
    expect(mockApi.upload.mock.calls[0][0]).toBe(`${base}/materials`);
    expect(mockApi.upload.mock.calls[0][1].get("file")).toBe(file);
    await user.click(screen.getByRole("button", { name: "分析所选材料" }));
    expect(mockApi.post).toHaveBeenCalledWith(
      `${base}/requirement-analyses`,
      { material_ids: ["m1"] },
      expect.any(String),
    );
  });

  it("isolates cached data and selection when switching projects", async () => {
    const user = userEvent.setup();
    stubGet({
      [`${base}/materials`]: [material],
      [`${base}/requirements`]: [requirement],
    });
    const view = renderWithProviders(
      <ProjectRequirementsPanel key="a" projectId="project-a" />,
    );
    await user.click(await screen.findByRole("checkbox"));
    expect(screen.getByText("合同原始条款")).toBeInTheDocument();
    view.rerender(<ProjectRequirementsPanel key="b" projectId="project-b" />);
    await screen.findByText("暂无材料");
    expect(screen.queryByText("合同原始条款")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "分析所选材料" })).toBeDisabled();
    expect(mockApi.get).toHaveBeenCalledWith(
      "/admin/projects/project-b/requirements",
    );
    expect(
      view.queryClient.getQueryData(
        queryKeys.adminRequirements("project-b", "requirements"),
      ),
    ).toEqual([]);
  });

  it("shows analysis failures and retries the selected material set", async () => {
    const user = userEvent.setup();
    stubGet({
      [`${base}/materials`]: [material],
      [`${base}/requirement-analyses`]: [
        {
          id: "a1",
          status: "failed",
          error: "模型暂不可用",
          created_at: requirement.created_at,
        },
      ],
    });
    mockApi.post.mockResolvedValue({ id: "a2", status: "pending" });
    renderWithProviders(<ProjectRequirementsPanel projectId="project-a" />);
    expect(await screen.findByText("模型暂不可用")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "重新分析所选材料" }),
    ).toBeDisabled();
    await user.click(screen.getByRole("checkbox"));
    await user.click(screen.getByRole("button", { name: "重新分析所选材料" }));
    expect(mockApi.post).toHaveBeenCalledWith(
      `${base}/requirement-analyses`,
      { material_ids: ["m1"] },
      expect.any(String),
    );
  });

  it("bounds active analysis polling and allows a manual restart", async () => {
    vi.useFakeTimers();
    stubGet({
      [`${base}/requirement-analyses`]: [
        {
          id: "a1",
          status: "running",
          error: null,
          created_at: requirement.created_at,
        },
      ],
    });
    const view = renderWithProviders(
      <ProjectRequirementsPanel projectId="project-a" />,
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(300_000);
    });
    expect(screen.getByText(/自动刷新已暂停/)).toBeInTheDocument();
    const count = mockApi.get.mock.calls.filter(([path]) => path.endsWith("requirement-analyses")).length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(mockApi.get.mock.calls.filter(([path]) => path.endsWith("requirement-analyses"))).toHaveLength(count);
    fireEvent.click(screen.getByRole("button", { name: "刷新需求" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2010);
    });
    expect(mockApi.get.mock.calls.length).toBeGreaterThan(count);
    view.unmount();
  });

  it("refreshes generated drafts when analysis succeeds and stops polling", async () => {
    vi.useFakeTimers();
    let succeeded = false;
    mockApi.get.mockImplementation((path: string) =>
      Promise.resolve(
        path.endsWith("requirement-analyses")
          ? [
              {
                id: "a1",
                status: succeeded ? "succeeded" : "running",
                created_at: requirement.created_at,
              },
            ]
          : path.endsWith("requirements") && succeeded
            ? [requirement]
            : [],
      ),
    );
    const view = renderWithProviders(
      <ProjectRequirementsPanel projectId="project-a" />,
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10);
    });
    succeeded = true;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2100);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10);
    });
    expect(screen.getByText("合同原始条款")).toBeInTheDocument();
    const count = mockApi.get.mock.calls.filter(([path]) => path.endsWith("requirement-analyses")).length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(mockApi.get.mock.calls.filter(([path]) => path.endsWith("requirement-analyses"))).toHaveLength(count);
    view.unmount();
  });

  it("edits source-backed drafts with version and all editable fields", async () => {
    const user = userEvent.setup();
    const changed = vi.fn();
    mockApi.patch.mockResolvedValue({ ...requirement, version: 3 });
    renderWithProviders(
      <RequirementCard
        requirement={requirement}
        adminPath={`${base}/requirements`}
        onChanged={changed}
      />,
    );
    expect(screen.getByText("合同原始条款")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "编辑需求" }));
    await user.clear(screen.getByLabelText("标题"));
    await user.type(screen.getByLabelText("标题"), "修订需求");
    await user.type(screen.getByLabelText("澄清问题"), "待确认条款");
    await user.click(screen.getByRole("button", { name: "保存草稿" }));
    expect(mockApi.patch).toHaveBeenCalledWith(
      `${base}/requirements/req-1`,
      {
        version: 2,
        title: "修订需求",
        description: "需求描述",
        acceptance_criteria: "验收通过",
        clarification_questions: "待确认条款",
      },
      expect.any(String),
    );
    await waitFor(() => expect(changed).toHaveBeenCalled());
  });

  it.each([
    ["draft", "确认需求", "confirm"],
    ["draft", "排除需求", "exclude"],
    ["confirmed", "派发给负责人", "dispatch"],
  ] as const)("%s supports versioned %s", async (status, name, action) => {
    mockApi.post.mockResolvedValue(requirement);
    renderWithProviders(
      <RequirementCard
        requirement={{ ...requirement, status }}
        adminPath={`${base}/requirements`}
        onChanged={vi.fn()}
      />,
    );
    await userEvent.setup().click(screen.getByRole("button", { name }));
    expect(mockApi.post).toHaveBeenCalledWith(
      `${base}/requirements/req-1/${action}`,
      { version: 2 },
      expect.any(String),
    );
  });

  it.each([{ acceptance_criteria: "" }, { clarification_questions: "未解决" }])(
    "blocks confirmation until acceptance and clarification are ready: %j",
    (overrides) => {
      renderWithProviders(
        <RequirementCard
          requirement={{ ...requirement, ...overrides }}
          adminPath={`${base}/requirements`}
          onChanged={vi.fn()}
        />,
      );
      expect(screen.getByRole("button", { name: "确认需求" })).toBeDisabled();
      expect(
        screen.queryByRole("button", { name: "派发给负责人" }),
      ).not.toBeInTheDocument();
    },
  );

  it("shows leader feedback and keeps dispatched requirements immutable", () => {
    renderWithProviders(
      <RequirementCard
        requirement={{
          ...requirement,
          status: "dispatched",
          leader_note: "请明确交付范围",
        }}
        adminPath={`${base}/requirements`}
        onChanged={vi.fn()}
      />,
    );
    expect(screen.getByText("请明确交付范围")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "编辑需求" }),
    ).not.toBeInTheDocument();
  });

  it("guards the leader route without fetching requirements for a member", async () => {
    signInAs(makeMember({ role: "member" }));
    renderWithProviders(
      <Routes>
        <Route
          path="/project-requirements"
          element={<ProjectRequirementsPage />}
        />
        <Route path="/" element={<p>工作台</p>} />
      </Routes>,
      { route: "/project-requirements" },
    );
    expect(await screen.findByText("工作台")).toBeInTheDocument();
    expect(mockApi.get).not.toHaveBeenCalled();
  });

  it.each(["accept", "clarify"] as const)(
    "leader can %s assigned dispatched requirements without task writes or source display",
    async (action) => {
      const user = userEvent.setup();
      signInAs(makeMember({ role: "leader" }));
      stubGet({
        "/project-requirements": [{ ...requirement, status: "dispatched" }],
      });
      mockApi.post.mockResolvedValue(requirement);
      renderWithProviders(<ProjectRequirementsPage />);
      await screen.findByText("合同需求");
      expect(screen.queryByText("合同原始条款")).not.toBeInTheDocument();
      if (action === "clarify") {
        expect(screen.getByRole("button", { name: "请求澄清" })).toBeDisabled();
        await user.type(screen.getByLabelText("澄清说明"), "明确范围");
      }
      await user.click(
        screen.getByRole("button", {
          name: action === "accept" ? "接收需求" : "请求澄清",
        }),
      );
      expect(mockApi.post).toHaveBeenCalledWith(
        `/project-requirements/req-1/${action}`,
        { version: 2, ...(action === "clarify" ? { note: "明确范围" } : {}) },
        expect.any(String),
      );
      expect(mockApi.post).toHaveBeenCalledTimes(1);
    },
  );
});
