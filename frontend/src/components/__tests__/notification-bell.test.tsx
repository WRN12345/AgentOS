/** 顶栏下拉菜单可打开性回归：铃铛通知菜单点击后必须弹出内容。 */
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../services/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../services/api")>();
  const { mockApi } = await import("../../test/mock-api");
  return { ...actual, api: mockApi };
});

import { NotificationBell } from "../../features/notifications/NotificationBell";
import { mockApi } from "../../test/mock-api";
import { renderWithProviders } from "../../test/render";

describe("NotificationBell 下拉菜单", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockApi.get.mockResolvedValue({
      unread_count: 1,
      items: [
        {
          id: "n1",
          type: "transfer.requested",
          title: "新的转派申请",
          body: "alice 申请转派",
          link: "/approvals",
          is_read: false,
          created_at: "2026-07-29T00:00:00Z",
        },
      ],
    });
  });

  it("点击铃铛后弹出通知列表", async () => {
    const user = userEvent.setup();
    renderWithProviders(<NotificationBell />);
    await user.click(screen.getByRole("button", { name: "通知" }));
    expect(await screen.findByText("新的转派申请")).toBeInTheDocument();
  });
});
