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
import { makeMember, makeProject, makeUser, tokens } from "../../../test/fixtures";

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

  it("仅参与 1 个项目时自动选中，加载该项目成员身份并写入令牌", async () => {
    const user = userEvent.setup();
    const me = makeUser();
    const member = makeMember({ user_id: me.id });
    const project = makeProject();
    mockApi.post.mockResolvedValue(tokens);
    mockApi.get.mockImplementation((path: string) => {
      if (path === "/auth/me/projects") return Promise.resolve([project]);
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
    // 新时序：loadProjects → 单项目自动选中 → loadIdentity
    await waitFor(() => {
      expect(toast.success).toHaveBeenCalledWith("登录成功");
    });
    const state = useAuthStore.getState();
    expect(state.accessToken).toBe(tokens.access_token);
    expect(state.projects).toEqual([project]);
    expect(state.currentProject?.id).toBe(project.id);
    expect(state.member?.id).toBe(member.id);
  });

  it("不参与任何项目（全局管理员）时不选项目、不加载成员、不请求 /members", async () => {
    const user = userEvent.setup();
    const adminUser = makeUser({ is_admin: true });
    mockApi.post.mockResolvedValue(tokens);
    mockApi.get.mockImplementation((path: string) => {
      if (path === "/auth/me/projects") return Promise.resolve([]);
      if (path === "/auth/me") return Promise.resolve(adminUser);
      return Promise.reject(new Error(`未 mock 的 GET ${path}`));
    });

    renderWithProviders(<LoginPage />);
    await user.type(screen.getByLabelText("用户名"), "admin");
    await user.type(screen.getByLabelText("密码"), "secret-password");
    await user.click(screen.getByRole("button", { name: "登录" }));

    await waitFor(() => {
      expect(toast.success).toHaveBeenCalledWith("登录成功");
    });
    const state = useAuthStore.getState();
    expect(state.currentProject).toBeNull();
    expect(state.member).toBeNull();
    expect(state.user?.is_admin).toBe(true);
    // 未选项目时不得带 X-Project-Id 打 /members（后端缺 header 会 400）
    expect(mockApi.get).not.toHaveBeenCalledWith("/members", expect.anything());
  });

  it("重新登录且无项目时，清掉上次持久化的 currentProject，避免残留旧项目头", async () => {
    const user = userEvent.setup();
    const adminUser = makeUser({ is_admin: true });
    mockApi.post.mockResolvedValue(tokens);
    mockApi.get.mockImplementation((path: string) => {
      if (path === "/auth/me/projects") return Promise.resolve([]);
      if (path === "/auth/me") return Promise.resolve(adminUser);
      return Promise.reject(new Error(`未 mock 的 GET ${path}`));
    });
    // 模拟上次登录残留的持久化项目上下文（未登出直接访问 /login 重新登录）
    useAuthStore.getState().setCurrentProject(makeProject());

    renderWithProviders(<LoginPage />);
    await user.type(screen.getByLabelText("用户名"), "admin");
    await user.type(screen.getByLabelText("密码"), "secret-password");
    await user.click(screen.getByRole("button", { name: "登录" }));

    await waitFor(() => {
      expect(toast.success).toHaveBeenCalledWith("登录成功");
    });
    // 拉取项目列表即重建立项目上下文：旧 currentProject 与 member 一并清空
    expect(useAuthStore.getState().currentProject).toBeNull();
    expect(useAuthStore.getState().member).toBeNull();
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
