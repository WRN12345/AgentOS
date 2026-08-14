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
import ProjectPickerPage from "../ProjectPickerPage";
import { mockApi } from "../../../test/mock-api";
import { renderWithProviders, signInAs } from "../../../test/render";
import { makeMember, makeProject, makeUser } from "../../../test/fixtures";

/** 项目选择页（ticket 09）：列出我参与的项目 + 角色徽章，点选进入工作台。 */
describe("ProjectPickerPage 项目选择页", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  function renderPicker() {
    return renderWithProviders(
      <Routes>
        <Route path="/projects" element={<ProjectPickerPage />} />
        <Route path="/" element={<div data-testid="workspace">工作台</div>} />
        <Route path="/console" element={<div data-testid="console">控制台</div>} />
        <Route path="/login" element={<div data-testid="login">登录页</div>} />
      </Routes>,
      { route: "/projects" },
    );
  }

  it("进入时拉取最新项目列表，列出我参与的项目并带角色徽章", async () => {
    const projectA = makeProject({
      id: "project-a",
      name: "Alpha",
      role: "member",
    });
    const projectB = makeProject({
      id: "project-b",
      name: "Beta",
      role: "leader",
    });
    mockApi.get.mockImplementation((path: string) => {
      if (path === "/auth/me/projects") return Promise.resolve([projectA, projectB]);
      return Promise.resolve([]);
    });
    signInAs(null, makeUser());

    renderPicker();

    expect(await screen.findByText("Alpha")).toBeInTheDocument();
    expect(screen.getByText("Beta")).toBeInTheDocument();
    // 角色徽章：成员 / 负责人
    expect(screen.getByText("成员")).toBeInTheDocument();
    expect(screen.getByText("负责人")).toBeInTheDocument();
  });

  it("点选项目后进入工作台，并加载该项目成员身份", async () => {
    const user = userEvent.setup();
    const me = makeUser();
    const member = makeMember({ user_id: me.id });
    const project = makeProject({ name: "Alpha" });
    mockApi.get.mockImplementation((path: string) => {
      if (path === "/auth/me/projects") return Promise.resolve([project]);
      if (path === "/auth/me") return Promise.resolve(me);
      if (path === "/members") return Promise.resolve([member]);
      return Promise.resolve([]);
    });
    signInAs(null, me);

    renderPicker();
    await user.click(await screen.findByText("Alpha"));

    await waitFor(() => {
      expect(screen.getByTestId("workspace")).toBeInTheDocument();
    });
    const state = useAuthStore.getState();
    expect(state.currentProject?.id).toBe(project.id);
    expect(state.member?.id).toBe(member.id);
  });

  it("管理员进入选择页重定向管理控制台", async () => {
    mockApi.get.mockImplementation((path: string) => {
      if (path === "/auth/me/projects") return Promise.resolve([]);
      return Promise.resolve([]);
    });
    signInAs(null, makeUser({ is_admin: true }));

    renderPicker();
    expect(await screen.findByTestId("console")).toBeInTheDocument();
  });

  it("没有参与任何项目时展示空态提示", async () => {
    mockApi.get.mockImplementation((path: string) => {
      if (path === "/auth/me/projects") return Promise.resolve([]);
      return Promise.resolve([]);
    });
    signInAs(null, makeUser());

    renderPicker();
    expect(
      await screen.findByText("你还没有参与任何项目"),
    ).toBeInTheDocument();
  });

  it("点「登出」清空登录态并回到登录页", async () => {
    const user = userEvent.setup();
    mockApi.get.mockImplementation((path: string) => {
      if (path === "/auth/me/projects") return Promise.resolve([]);
      return Promise.resolve([]);
    });
    mockApi.post.mockResolvedValue({});
    signInAs(null, makeUser());

    renderPicker();
    await user.click(await screen.findByRole("button", { name: "登出" }));

    await waitFor(() => {
      expect(screen.getByTestId("login")).toBeInTheDocument();
    });
    expect(useAuthStore.getState().accessToken).toBeNull();
    expect(useAuthStore.getState().user).toBeNull();
  });
});
