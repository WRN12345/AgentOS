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
import { ApiError } from "../../../services/api";
import { useAuthStore } from "../../../app/store";
import LoginPage from "../LoginPage";
import { mockApi } from "../../../test/mock-api";
import { renderWithProviders } from "../../../test/render";
import { makeMember, makeUser, tokens } from "../../../test/fixtures";

/** 登录表单组件测试（18.2 节）：渲染、必填校验、提交调用 API、错误展示。 */
describe("LoginPage 登录表单", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("渲染用户名、密码输入框与登录按钮", () => {
    renderWithProviders(<LoginPage />);
    expect(screen.getByLabelText("用户名")).toBeInTheDocument();
    expect(screen.getByLabelText("密码")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "登录" })).toBeInTheDocument();
  });

  it("空表单提交时展示必填校验错误，且不调用登录接口", async () => {
    const user = userEvent.setup();
    renderWithProviders(<LoginPage />);

    await user.click(screen.getByRole("button", { name: "登录" }));

    expect(await screen.findByText("请输入用户名")).toBeInTheDocument();
    expect(screen.getByText("请输入密码")).toBeInTheDocument();
    expect(mockApi.post).not.toHaveBeenCalled();
  });

  it("提交后调用 /auth/login 并加载身份、写入令牌", async () => {
    const user = userEvent.setup();
    const me = makeUser();
    const member = makeMember({ user_id: me.id });
    mockApi.post.mockResolvedValue(tokens);
    mockApi.get.mockImplementation((path: string) => {
      if (path === "/auth/me") return Promise.resolve(me);
      if (path === "/members") return Promise.resolve([member]);
      return Promise.reject(new Error(`未 mock 的 GET ${path}`));
    });

    renderWithProviders(<LoginPage />);
    await user.type(screen.getByLabelText("用户名"), "alice");
    await user.type(screen.getByLabelText("密码"), "secret-password");
    await user.click(screen.getByRole("button", { name: "登录" }));

    await waitFor(() => {
      expect(mockApi.post).toHaveBeenCalledWith("/auth/login", {
        username: "alice",
        password: "secret-password",
      });
    });
    // loadIdentity：GET /auth/me + GET /members
    await waitFor(() => {
      expect(toast.success).toHaveBeenCalledWith("登录成功");
    });
    const state = useAuthStore.getState();
    expect(state.accessToken).toBe(tokens.access_token);
    expect(state.member?.id).toBe(member.id);
  });

  it("401 响应时展示「用户名或密码错误」", async () => {
    const user = userEvent.setup();
    mockApi.post.mockRejectedValue(
      new ApiError(401, {
        code: "INVALID_CREDENTIALS",
        message: "用户名或密码错误",
        request_id: "req-1",
      }),
    );

    renderWithProviders(<LoginPage />);
    await user.type(screen.getByLabelText("用户名"), "alice");
    await user.type(screen.getByLabelText("密码"), "wrong-password");
    await user.click(screen.getByRole("button", { name: "登录" }));

    await waitFor(() => {
      expect(toast.error).toHaveBeenCalledWith("用户名或密码错误");
    });
  });

  it("其他接口错误时展示带后端 message 的失败提示", async () => {
    const user = userEvent.setup();
    mockApi.post.mockRejectedValue(
      new ApiError(500, {
        code: "INTERNAL",
        message: "服务内部错误",
        request_id: "req-2",
      }),
    );

    renderWithProviders(<LoginPage />);
    await user.type(screen.getByLabelText("用户名"), "alice");
    await user.type(screen.getByLabelText("密码"), "secret-password");
    await user.click(screen.getByRole("button", { name: "登录" }));

    await waitFor(() => {
      expect(toast.error).toHaveBeenCalledWith("登录失败：服务内部错误");
    });
  });
});
