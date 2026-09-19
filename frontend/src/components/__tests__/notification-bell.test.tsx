/** 顶栏下拉菜单可打开性回归：铃铛通知菜单点击后必须弹出内容。 */
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "sonner";

vi.mock("sonner", () => ({ toast: { error: vi.fn() } }));

vi.mock("../../services/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../services/api")>();
  const { mockApi } = await import("../../test/mock-api");
  return { ...actual, api: mockApi };
});

import { NotificationBell } from "../../features/notifications/NotificationBell";
import { mockApi } from "../../test/mock-api";
import { renderWithProviders, signInAs } from "../../test/render";
import { useAuthStore } from "../../app/store";
import { makeLeader, makeMember, makeProject, makeUser } from "../../test/fixtures";
import type { NotificationList } from "../../types";

describe("NotificationBell 下拉菜单", () => {
  let unread: NotificationList;
  beforeEach(() => {
    vi.resetAllMocks();
    mockApi.post.mockResolvedValue(undefined);
    unread = {
      unread_count: 1,
      items: [
        {
          id: "n1",
          type: "transfer.requested",
          title: "新的转派申请",
          body: "alice 申请转派",
          link: "/approvals",
          is_read: false,
          read_at: null,
          created_at: "2026-07-29T00:00:00Z",
        },
      ],
    };
    mockApi.get.mockResolvedValue(unread);
  });

  it("点击铃铛后弹出通知列表", async () => {
    const user = userEvent.setup();
    renderWithProviders(<NotificationBell />);
    await user.click(screen.getByRole("button", { name: "通知" }));
    expect(await screen.findByText("新的转派申请")).toBeInTheDocument();
  });

  it("批量处理超过当前页的未读并刷新样式，保持面板打开", async () => {
    mockApi.get.mockResolvedValue({ ...unread, unread_count: 123 });
    const user = userEvent.setup();
    renderWithProviders(<NotificationBell />);
    await user.click(screen.getByRole("button", { name: "通知" }));
    expect(await screen.findByText("123 条未读")).toBeInTheDocument();
    mockApi.get.mockResolvedValue({
      unread_count: 0,
      items: unread.items.map((item) => ({ ...item, is_read: true })),
    });
    await user.click(screen.getByRole("button", { name: "全部已读" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "全部已读" })).toBeDisabled());
    expect(mockApi.post).toHaveBeenCalledExactlyOnceWith("/notifications/read-all");
    expect(screen.queryByText("123 条未读")).not.toBeInTheDocument();
    expect(screen.queryByText("99+")).not.toBeInTheDocument();
    expect(screen.getByText("新的转派申请")).not.toHaveClass("font-medium");
    expect(screen.getByRole("button", { name: "通知" })).toHaveAttribute("aria-expanded", "true");
  });

  it("处理中禁用重复提交，失败保留未读并提示", async () => {
    let rejectRequest!: (error: Error) => void;
    mockApi.post.mockImplementation(() => new Promise((_resolve, reject) => { rejectRequest = reject; }));
    const user = userEvent.setup();
    renderWithProviders(<NotificationBell />);
    await user.click(screen.getByRole("button", { name: "通知" }));
    await screen.findByText("1 条未读");
    await user.click(screen.getByRole("button", { name: "全部已读" }));
    const pending = screen.getByRole("button", { name: "处理中..." });
    expect(pending).toBeDisabled();
    await user.click(pending);
    expect(mockApi.post).toHaveBeenCalledTimes(1);
    rejectRequest(new Error("offline"));
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith("全部已读失败，请稍后重试"));
    expect(screen.getByText("1 条未读")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "全部已读" })).toBeEnabled();
  });

  it("没有未读时禁用按钮", async () => {
    mockApi.get.mockResolvedValue({ unread_count: 0, items: [] });
    const user = userEvent.setup();
    renderWithProviders(<NotificationBell />);
    await user.click(screen.getByRole("button", { name: "通知" }));
    expect(await screen.findByText("暂无通知")).toBeInTheDocument();
    const button = screen.getByRole("button", { name: "全部已读" });
    expect(button).toBeDisabled();
    await user.click(button);
    expect(mockApi.post).not.toHaveBeenCalled();
  });

  it.each([
    { name: "管理员", member: makeMember(), user: makeUser({ is_admin: true }) },
    { name: "负责人", member: makeLeader(), user: makeUser() },
    { name: "成员", member: makeMember(), user: makeUser() },
  ])("$name 可使用全部已读", async ({ member, user: identity }) => {
    signInAs(member, identity, makeProject());
    const user = userEvent.setup();
    renderWithProviders(<NotificationBell />);
    await user.click(screen.getByRole("button", { name: "通知" }));
    await screen.findByText("1 条未读");
    await user.click(screen.getByRole("button", { name: "全部已读" }));
    expect(mockApi.post).toHaveBeenCalledExactlyOnceWith("/notifications/read-all");
  });

  it("切换项目后仍失效操作原项目的缓存", async () => {
    useAuthStore.getState().setCurrentProject(makeProject());
    let resolveRequest!: () => void;
    mockApi.post.mockImplementation(() => new Promise<void>((resolve) => { resolveRequest = resolve; }));
    const user = userEvent.setup();
    const { queryClient } = renderWithProviders(<NotificationBell />);
    const invalidate = vi.spyOn(queryClient, "invalidateQueries");
    await user.click(screen.getByRole("button", { name: "通知" }));
    await screen.findByText("1 条未读");
    await user.click(screen.getByRole("button", { name: "全部已读" }));
    useAuthStore.getState().setCurrentProject(makeProject({ id: "project-2" }));
    resolveRequest();
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith(
      { queryKey: ["project-1", "notifications"] }, { throwOnError: true },
    ));
  });

  it("写入成功但刷新失败时明确提示已标记", async () => {
    const user = userEvent.setup();
    renderWithProviders(<NotificationBell />);
    await user.click(screen.getByRole("button", { name: "通知" }));
    await screen.findByText("1 条未读");
    mockApi.get.mockRejectedValue(new Error("offline"));
    await user.click(screen.getByRole("button", { name: "全部已读" }));
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith("已标记全部已读，通知刷新失败，请稍后重试"));
  });

  it("单条已读仍关闭面板", async () => {
    const user = userEvent.setup();
    renderWithProviders(<NotificationBell />);
    await user.click(screen.getByRole("button", { name: "通知" }));
    await user.click(await screen.findByText("新的转派申请"));
    expect(mockApi.post).toHaveBeenCalledExactlyOnceWith("/notifications/n1/read");
    expect(screen.getByRole("button", { name: /通知/ })).toHaveAttribute("aria-expanded", "false");
  });
});
