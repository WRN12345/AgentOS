import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route, Routes } from "react-router-dom";
import { toast } from "sonner";
vi.mock("../../../services/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../services/api")>();
  const { mockApi } = await import("../../../test/mock-api");
  return { ...actual, api: mockApi };
});
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));
import { HandoffSection } from "../HandoffSection";
import WorkItemDetailPage from "../WorkItemDetailPage";
import { DeliverableSection } from "../../deliverables/DeliverableSection";
import { HandoffInbox } from "../../dashboard/HandoffInbox";
import { ACTIVE_STATUSES } from "../../dashboard/shared";
import { mockApi } from "../../../test/mock-api";
import { renderWithProviders, signInAs } from "../../../test/render";
import { makeDeliverable, makeLeader, makeMember, makeWorkItem } from "../../../test/fixtures";
import { ApiError, VERSION_CONFLICT_MESSAGE } from "../../../services/api";
import type { Handoff } from "../../../types";

const source = makeWorkItem({ title: "接口设计", version: 4 });
const recipient = { id: "member-2", display_name: "小李" };
const target = makeWorkItem({ id: "wi-2", title: "接口实现", status: "READY", version: 7, assignee: recipient });
const delivery = makeDeliverable({ id: "del-3", type: "text", content: "接口契约与验收结果", version: 3 });
const pending: Handoff = {
  id: "handoff-1", source_work_item: { ...source, status: "WAITING_ACCEPTANCE", version: 5 },
  target_work_item: target, deliverable: delivery, sender: source.assignee, recipient,
  status: "pending", note: "请据此实现", response_note: null, version: 1,
  created_at: "2026-09-19T00:00:00Z", updated_at: "2026-09-19T00:00:00Z", responded_at: null,
};
let records: Handoff[];

beforeEach(() => {
  vi.clearAllMocks();
  records = [];
  signInAs(makeMember());
  mockApi.get.mockImplementation((path: string) => {
    if (path === "/work-items/wi-1") return Promise.resolve(source);
    if (path === "/work-items/wi-2") return Promise.resolve(target);
    if (path.endsWith("/handoffs") || path.startsWith("/handoffs?")) return Promise.resolve(records);
    if (path.startsWith("/handoff-targets?")) return Promise.resolve([target]);
    if (path.endsWith("/deliverables")) return Promise.resolve([delivery]);
    return Promise.resolve([]);
  });
  mockApi.post.mockResolvedValue(pending);
});

async function chooseTarget() {
  const user = userEvent.setup();
  await waitFor(() => expect(screen.getByRole("combobox", { name: "接续任务" })).toBeEnabled());
  await user.click(screen.getByRole("combobox", { name: "接续任务" }));
  await user.click(await screen.findByRole("option", { name: "接口实现 · 小李" }));
  return user;
}

describe("任务移交", () => {
  it("previews latest delivery and recipient and submits both task versions", async () => {
    renderWithProviders(<HandoffSection workItem={source} />);
    expect(await screen.findByText("接口契约与验收结果")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "确认移交" })).toBeDisabled();
    const user = await chooseTarget();
    expect(screen.getByText("接收人：小李 · 接续任务 v7")).toBeInTheDocument();
    await user.type(screen.getByLabelText("移交说明（选填）"), " 按文档实现 ");
    await user.click(screen.getByRole("button", { name: "确认移交" }));
    await waitFor(() => expect(mockApi.post).toHaveBeenCalledWith("/work-items/wi-1/handoffs", {
      version: 4, target_work_item_id: "wi-2", target_version: 7, deliverable_id: "del-3", note: "按文档实现",
    }, expect.any(String)));
  });

  it("recipient accepts exact delivery and does not automatically start target", async () => {
    records = [pending];
    signInAs(makeMember(recipient));
    mockApi.post.mockImplementation(async () => {
      records = [{ ...pending, status: "accepted", version: 2 }];
      return records[0];
    });
    renderWithProviders(<HandoffSection workItem={target} />);
    expect(await screen.findByText("交付物 v3")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "确认接收" }));
    await screen.findByText("已接收");
    expect(mockApi.post).toHaveBeenCalledWith("/handoffs/handoff-1/accept", { version: 1 }, expect.any(String));
    expect(mockApi.post).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("button", { name: "确认接收" })).not.toBeInTheDocument();
  });

  it("validates the send note length through the form schema", async () => {
    renderWithProviders(<HandoffSection workItem={source} />);
    const user = await chooseTarget();
    const note = screen.getByLabelText("移交说明（选填）");
    expect(note).toHaveAttribute("maxlength", "10000");
    fireEvent.change(note, { target: { value: "a".repeat(10001) } });
    await user.click(screen.getByRole("button", { name: "确认移交" }));
    expect(await screen.findByText("移交说明最多 10000 字")).toBeInTheDocument();
    expect(note).toHaveAttribute("aria-invalid", "true");
    expect(mockApi.post).not.toHaveBeenCalled();
  });

  it.each(["确认接收", "请求补正"])("validates response length before %s", async (action) => {
    records = [pending];
    signInAs(makeMember(recipient));
    renderWithProviders(<HandoffSection workItem={target} />);
    const note = await screen.findByLabelText("接收反馈（请求补正时必填）");
    expect(note).toHaveAttribute("maxlength", "10000");
    fireEvent.change(note, { target: { value: "a".repeat(10001) } });
    await userEvent.click(screen.getByRole("button", { name: action }));
    expect(await screen.findByText("接收反馈最多 10000 字")).toBeInTheDocument();
    expect(mockApi.post).not.toHaveBeenCalled();
  });

  it("keeps response fields and actions disabled while pending", async () => {
    records = [pending];
    signInAs(makeMember(recipient));
    mockApi.post.mockReturnValue(new Promise(() => {}));
    renderWithProviders(<HandoffSection workItem={target} />);
    const note = await screen.findByLabelText("接收反馈（请求补正时必填）");
    fireEvent.change(note, { target: { value: "   " } });
    expect(screen.getByRole("button", { name: "请求补正" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "确认接收" }));
    await waitFor(() => expect(note).toBeDisabled());
    expect(screen.getByRole("button", { name: "确认接收" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "请求补正" })).toBeDisabled();
  });

  it("keeps send fields and submission disabled while pending", async () => {
    mockApi.post.mockReturnValue(new Promise(() => {}));
    renderWithProviders(<HandoffSection workItem={source} />);
    const user = await chooseTarget();
    await user.click(screen.getByRole("button", { name: "确认移交" }));
    await waitFor(() => expect(screen.getByLabelText("移交说明（选填）")).toBeDisabled());
    expect(screen.getByRole("combobox", { name: "接续任务" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "移交中…" })).toBeDisabled();
  });

  it("schema requires correction note even on direct form submission", async () => {
    records = [pending];
    signInAs(makeMember(recipient));
    renderWithProviders(<HandoffSection workItem={target} />);
    const note = await screen.findByLabelText("接收反馈（请求补正时必填）");
    fireEvent.change(note, { target: { value: "a".repeat(10001) } });
    await userEvent.click(screen.getByRole("button", { name: "请求补正" }));
    await screen.findByText("接收反馈最多 10000 字");
    fireEvent.change(note, { target: { value: "   " } });
    fireEvent.submit(note.closest("form")!);
    expect(await screen.findByText("请填写补正意见")).toBeInTheDocument();
    expect(mockApi.post).not.toHaveBeenCalled();
  });

  it("requires a visible correction note and preserves response in history", async () => {
    records = [pending];
    signInAs(makeMember(recipient));
    mockApi.post.mockImplementation(async () => {
      records = [{ ...pending, status: "changes_requested", response_note: "补充错误码", version: 2 }];
      return records[0];
    });
    renderWithProviders(<HandoffSection workItem={target} />);
    const correction = await screen.findByRole("button", { name: "请求补正" });
    expect(correction).toBeDisabled();
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("接收反馈（请求补正时必填）"), "补充错误码");
    await user.click(correction);
    await screen.findByText("接收反馈：补充错误码");
    expect(mockApi.post).toHaveBeenCalledWith("/handoffs/handoff-1/request-changes", { version: 1, note: "补充错误码" }, expect.any(String));
  });

  it.each([makeLeader(), makeMember()])("sender and unrelated leader have read-only history ($id)", async (member) => {
    records = [pending];
    signInAs(member);
    renderWithProviders(<HandoffSection workItem={{ ...source, status: "WAITING_ACCEPTANCE" }} />);
    await screen.findByText("接口契约与验收结果");
    expect(screen.queryByRole("button", { name: "确认接收" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "请求补正" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "确认移交" })).not.toBeInTheDocument();
  });

  it("leader who is the recipient can accept", async () => {
    records = [pending];
    signInAs(makeLeader(recipient));
    renderWithProviders(<HandoffSection workItem={target} />);
    expect(await screen.findByRole("button", { name: "确认接收" })).toBeEnabled();
  });

  it("downloads the exact handed-off file through the authenticated endpoint", async () => {
    records = [{ ...pending, deliverable: { ...delivery, type: "file", content: null, file: {
      id: "file-version-3", original_filename: "api.md", size_bytes: 123, mime_type: "text/markdown", sha256: "a".repeat(64),
    } } }];
    signInAs(makeMember(recipient));
    mockApi.downloadFile.mockRejectedValue(new ApiError(403, { code: "FORBIDDEN", message: "无权限", request_id: "r" }));
    renderWithProviders(<HandoffSection workItem={target} />);
    expect(await screen.findByText("api.md")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "下载" }));
    expect(mockApi.downloadFile).toHaveBeenCalledWith("/files/file-version-3/download");
    expect(mockApi.get).not.toHaveBeenCalledWith("/work-items/wi-1/deliverables");
  });

  it("unrelated leader cannot send on behalf of source owner", async () => {
    signInAs(makeLeader());
    renderWithProviders(<HandoffSection workItem={source} />);
    await screen.findByText("暂无移交记录");
    expect(screen.queryByRole("combobox", { name: "接续任务" })).not.toBeInTheDocument();
    expect(mockApi.get).not.toHaveBeenCalledWith("/handoff-targets?source_work_item_id=wi-1");
  });

  it("refreshes source, target and delivery on version conflict and requires reselection", async () => {
    mockApi.post.mockRejectedValue(new ApiError(409, { code: "WORK_ITEM_VERSION_CONFLICT", message: "conflict", request_id: "r" }));
    renderWithProviders(<HandoffSection workItem={source} />);
    const user = await chooseTarget();
    await user.click(screen.getByRole("button", { name: "确认移交" }));
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith(VERSION_CONFLICT_MESSAGE));
    expect(screen.getByRole("button", { name: "确认移交" })).toBeDisabled();
    expect(mockApi.get.mock.calls.filter(([path]) => path === "/handoff-targets?source_work_item_id=wi-1").length).toBeGreaterThan(1);
    expect(mockApi.get.mock.calls.filter(([path]) => path === "/work-items/wi-1/deliverables").length).toBeGreaterThan(1);
  });

  it("recipient conflict refreshes history without claiming success", async () => {
    records = [pending];
    signInAs(makeMember(recipient));
    mockApi.post.mockRejectedValue(new ApiError(409, { code: "HANDOFF_VERSION_CONFLICT", message: "conflict", request_id: "r" }));
    renderWithProviders(<HandoffSection workItem={target} />);
    await userEvent.click(await screen.findByRole("button", { name: "确认接收" }));
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith(VERSION_CONFLICT_MESSAGE));
    expect(toast.success).not.toHaveBeenCalled();
    expect(mockApi.get.mock.calls.filter(([path]) => path === "/work-items/wi-2/handoffs").length).toBeGreaterThan(1);
  });

  it("requires latest delivery before sending", async () => {
    mockApi.get.mockResolvedValue([]);
    renderWithProviders(<HandoffSection workItem={source} />);
    await screen.findByText("请先提交交付物，再发起移交");
    expect(screen.getByRole("button", { name: "确认移交" })).toBeDisabled();
  });

  it("waiting source keeps delivery history but cannot submit another version", async () => {
    renderWithProviders(<DeliverableSection workItem={{ ...source, status: "WAITING_ACCEPTANCE" }} />);
    await screen.findByText("接口契约与验收结果");
    expect(screen.queryByRole("button", { name: "提交交付" })).not.toBeInTheDocument();
    expect(ACTIVE_STATUSES).toContain("WAITING_ACCEPTANCE");
  });

  it("inbox links to target only and includes source and delivery version", async () => {
    records = [pending];
    renderWithProviders(<HandoffInbox />);
    const link = await screen.findByRole("link", { name: "接收至：接口实现" });
    expect(link).toHaveAttribute("href", "/work-items/wi-2#handoff-section");
    expect(screen.getByText("爱丽丝 · 接口设计")).toBeInTheDocument();
    expect(screen.getByText("交付物 v3")).toBeInTheDocument();
    expect(screen.getAllByRole("link")).toHaveLength(1);
  });

  it("pending incoming disables target start and acceptance leaves manual start available", async () => {
    records = [pending];
    signInAs(makeMember(recipient));
    mockApi.post.mockImplementation(async () => {
      records = [{ ...pending, status: "accepted", version: 2 }];
      return records[0];
    });
    renderWithProviders(<Routes><Route path="/work-items/:id" element={<WorkItemDetailPage />} /></Routes>, { route: "/work-items/wi-2" });
    expect(await screen.findByRole("button", { name: "开始" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "申请转派" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "提交交付" })).not.toBeInTheDocument();
    await userEvent.click(await screen.findByRole("button", { name: "确认接收" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "开始" })).toBeEnabled());
    expect(mockApi.post).toHaveBeenCalledTimes(1);
  });

  it("task primary entry opens handoff instead of submitting leader review", async () => {
    renderWithProviders(<Routes><Route path="/work-items/:id" element={<WorkItemDetailPage />} /></Routes>, { route: "/work-items/wi-1" });
    expect(await screen.findByRole("link", { name: "移交任务" })).toHaveAttribute("href", "#handoff-section");
    expect(screen.queryByRole("button", { name: "提交审核" })).not.toBeInTheDocument();
  });
});
