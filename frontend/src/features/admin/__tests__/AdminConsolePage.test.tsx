import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route, Routes } from "react-router-dom";

vi.mock("../../../services/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../services/api")>();
  const { mockApi } = await import("../../../test/mock-api");
  return { ...actual, api: mockApi };
});

import { useAuthStore } from "../../../app/store";
import AdminConsolePage from "../AdminConsolePage";
import { mockApi } from "../../../test/mock-api";
import { renderWithProviders, signInAs } from "../../../test/render";
import { makeUser } from "../../../test/fixtures";

/**
 * 管理控制台占位页（ticket 09 分流落地；ticket 10 填充功能）。
 * 是管理员登录后的唯一去处，必须提供登出入口，否则管理员会卡死。
 */
describe("AdminConsolePage 管理控制台", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  function renderConsole() {
    return renderWithProviders(
      <Routes>
        <Route path="/console" element={<AdminConsolePage />} />
        <Route path="/login" element={<div data-testid="login">登录页</div>} />
      </Routes>,
      { route: "/console" },
    );
  }

  it("渲染占位标题与当前管理员用户名", () => {
    signInAs(null, makeUser({ is_admin: true, username: "root" }));

    renderConsole();

    expect(screen.getByText("管理控制台")).toBeInTheDocument();
    expect(
      screen.getByText(/欢迎，root。/),
    ).toBeInTheDocument();
  });

  it("点「登出」撤销令牌并回到登录页", async () => {
    const user = userEvent.setup();
    mockApi.post.mockResolvedValue({});
    signInAs(null, makeUser({ is_admin: true }));

    renderConsole();
    await user.click(screen.getByRole("button", { name: "登出" }));

    await waitFor(() => {
      expect(screen.getByTestId("login")).toBeInTheDocument();
    });
    const state = useAuthStore.getState();
    expect(state.accessToken).toBeNull();
    expect(state.refreshToken).toBeNull();
    expect(state.user).toBeNull();
    // 登出会尽力撤销服务端 Refresh Token
    expect(mockApi.post).toHaveBeenCalledWith("/auth/logout", {
      refresh_token: "test-refresh-token",
    });
  });
});
