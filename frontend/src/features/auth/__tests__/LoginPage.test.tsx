import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route, Routes } from "react-router-dom";

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

/** 登录页组件测试（18.2 节 + ticket 09 分流时序）。 */
describe("LoginPage 登录表单", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  function renderLogin() {
    return renderWithProviders(
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/" element={<div data-testid="workspace">工作台</div>} />
        <Route path="/projects" element={<div data-testid="picker">选择页</div>} />
        <Route path="/console" element={<div data-testid="console">控制台</div>} />
      </Routes>,
      { route: "/login" },
    );
  }

  /** 填写表单并点击登录。 */
  async function submitLogin(user: ReturnType<typeof userEvent.setup>) {
    await user.type(screen.getByLabelText("用户名"), "alice");
    await user.type(screen.getByLabelText("密码"), "secret-password");
    await user.click(screen.getByRole("button", { name: "登录" }));
  }

  it("渲染用户名、密码输入框与登录按钮", () => {
    renderLogin();
    expect(screen.getByLabelText("用户名")).toBeInTheDocument();
    expect(screen.getByLabelText("密码")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "登录" })).toBeInTheDocument();
  });

  it("空表单提交时展示必填校验错误，且不调用登录接口", async () => {
    const user = userEvent.setup();
    renderLogin();

    await user.click(screen.getByRole("button", { name: "登录" }));

    expect(await screen.findByText("请输入用户名")).toBeInTheDocument();
    expect(screen.getByText("请输入密码")).toBeInTheDocument();
    expect(mockApi.post).not.toHaveBeenCalled();
  });

  it("普通用户无上次记忆时进入项目选择页，不自动选项目、不加载成员", async () => {
    const user = userEvent.setup();
    const me = makeUser();
    const project = makeProject();
    mockApi.post.mockResolvedValue(tokens);
    mockApi.get.mockImplementation((path: string) => {
      if (path === "/auth/me/projects") return Promise.resolve([project]);
      if (path === "/auth/me") return Promise.resolve(me);
      return Promise.reject(new Error(`未 mock 的 GET ${path}`));
    });

    renderLogin();
    await submitLogin(user);

    // 分流到项目选择页
    expect(await screen.findByTestId("picker")).toBeInTheDocument();
    const state = useAuthStore.getState();
    expect(state.projects).toEqual([project]);
    expect(state.currentProject).toBeNull();
    expect(state.member).toBeNull();
    // 未选项目时不得带 X-Project-Id 打 /members（后端缺 header 会 400）
    expect(mockApi.get).not.toHaveBeenCalledWith("/members", expect.anything());
  });

  it("24h 记忆窗口内记得上次项目时直接进入工作台并加载成员", async () => {
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
    // 模拟上次登录选过这个项目（时间戳为当前时刻，24h 窗口内）
    useAuthStore.getState().setCurrentProject(project);

    renderLogin();
    await submitLogin(user);

    expect(await screen.findByTestId("workspace")).toBeInTheDocument();
    const state = useAuthStore.getState();
    expect(state.currentProject?.id).toBe(project.id);
    expect(state.member?.id).toBe(member.id);
  });

  it("上次项目记忆已过期（超过 24h）时进入项目选择页重新分流", async () => {
    const user = userEvent.setup();
    const me = makeUser();
    const project = makeProject();
    mockApi.post.mockResolvedValue(tokens);
    mockApi.get.mockImplementation((path: string) => {
      if (path === "/auth/me/projects") return Promise.resolve([project]);
      if (path === "/auth/me") return Promise.resolve(me);
      return Promise.reject(new Error(`未 mock 的 GET ${path}`));
    });
    // 上次选择时间戳拨到 25 小时前，模拟记忆过期
    useAuthStore.getState().setCurrentProject(project);
    useAuthStore.setState({
      projectSelectedAt: Date.now() - 25 * 60 * 60 * 1000,
    });

    renderLogin();
    await submitLogin(user);

    expect(await screen.findByTestId("picker")).toBeInTheDocument();
    expect(useAuthStore.getState().currentProject).toBeNull();
    expect(useAuthStore.getState().member).toBeNull();
  });

  it("管理员登录进入管理控制台，不选项目、不加载成员、不请求 /members", async () => {
    const user = userEvent.setup();
    const adminUser = makeUser({ is_admin: true });
    mockApi.post.mockResolvedValue(tokens);
    mockApi.get.mockImplementation((path: string) => {
      if (path === "/auth/me/projects") return Promise.resolve([]);
      if (path === "/auth/me") return Promise.resolve(adminUser);
      return Promise.reject(new Error(`未 mock 的 GET ${path}`));
    });

    renderLogin();
    await submitLogin(user);

    expect(await screen.findByTestId("console")).toBeInTheDocument();
    const state = useAuthStore.getState();
    expect(state.user?.is_admin).toBe(true);
    expect(state.currentProject).toBeNull();
    expect(state.member).toBeNull();
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

    renderLogin();
    await submitLogin(user);

    await waitFor(() => {
      expect(screen.getByTestId("console")).toBeInTheDocument();
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

    renderLogin();
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

    renderLogin();
    await user.type(screen.getByLabelText("用户名"), "alice");
    await user.type(screen.getByLabelText("密码"), "secret-password");
    await user.click(screen.getByRole("button", { name: "登录" }));

    await waitFor(() => {
      expect(toast.error).toHaveBeenCalledWith("登录失败：服务内部错误");
    });
  });
});
