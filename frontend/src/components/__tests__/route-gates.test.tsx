import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { Route, Routes } from "react-router-dom";
import { useAuthStore } from "../../app/store";
import { renderWithProviders, signInAs } from "../../test/render";
import { makeMember, makeProject, makeUser } from "../../test/fixtures";
import AdminOnly from "../AdminOnly";
import ProjectGate from "../ProjectGate";
import { mockApi } from "../../test/mock-api";

vi.mock("../../services/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../services/api")>();
  const { mockApi: replacement } = await import("../../test/mock-api");
  return { ...actual, api: replacement };
});

beforeEach(() => {
  vi.clearAllMocks();
  mockApi.get.mockImplementation(() =>
    Promise.resolve(useAuthStore.getState().projects ?? []),
  );
});

/** 路由守卫测试：登录后按身份与项目上下文分流（ticket 09）。 */
describe("ProjectGate 工作台守卫", () => {
  function renderGate() {
    return renderWithProviders(
      <Routes>
        <Route
          path="/"
          element={
            <ProjectGate>
              <div data-testid="workspace">工作台</div>
            </ProjectGate>
          }
        />
        <Route path="/console" element={<div data-testid="console">控制台</div>} />
        <Route path="/projects" element={<div data-testid="picker">选择页</div>} />
      </Routes>,
      { route: "/" },
    );
  }

  it("管理员登录后重定向管理控制台，不进入工作台", () => {
    signInAs(null, makeUser({ is_admin: true }));
    renderGate();
    expect(screen.getByTestId("console")).toBeInTheDocument();
    expect(screen.queryByTestId("workspace")).not.toBeInTheDocument();
  });

  it("普通用户未选项目时重定向项目选择页", () => {
    signInAs(null, makeUser());
    renderGate();
    expect(screen.getByTestId("picker")).toBeInTheDocument();
    expect(screen.queryByTestId("workspace")).not.toBeInTheDocument();
  });

  it("普通用户已选项目（24h 内且服务端资格仍有效）时放行工作台", async () => {
    signInAs(makeMember(), makeUser(), makeProject());
    renderGate();
    expect(await screen.findByTestId("workspace")).toBeInTheDocument();
  });

  it("普通用户上次项目记忆已过期（超过 24h）时回到项目选择页", async () => {
    signInAs(makeMember(), makeUser(), makeProject());
    // 把上次选择时间戳拨到 25 小时前，模拟记忆窗口过期
    useAuthStore.setState({ projectSelectedAt: Date.now() - 25 * 60 * 60 * 1000 });
    renderGate();
    expect(await screen.findByTestId("picker")).toBeInTheDocument();
    expect(screen.queryByTestId("workspace")).not.toBeInTheDocument();
  });

  it("服务端复验发现当前项目已不在参与列表时回到项目选择页", async () => {
    signInAs(makeMember(), makeUser(), makeProject());
    mockApi.get.mockResolvedValueOnce([
      makeProject({ id: "project-other", name: "Beta" }),
    ]);
    renderGate();
    expect(await screen.findByTestId("picker")).toBeInTheDocument();
    expect(screen.queryByTestId("workspace")).not.toBeInTheDocument();
  });

  it("当前项目成员身份缺失或已禁用时回到项目选择页", async () => {
    signInAs(null, makeUser(), makeProject());
    renderGate();
    expect(await screen.findByTestId("picker")).toBeInTheDocument();
    expect(screen.queryByTestId("workspace")).not.toBeInTheDocument();
  });
});

describe("AdminOnly 管理台守卫", () => {
  function renderConsole() {
    return renderWithProviders(
      <Routes>
        <Route
          path="/console"
          element={
            <AdminOnly>
              <div data-testid="console">控制台</div>
            </AdminOnly>
          }
        />
        <Route path="/" element={<div data-testid="home">工作台</div>} />
      </Routes>,
      { route: "/console" },
    );
  }

  it("非管理员访问管理台重定向离开", () => {
    signInAs(makeMember(), makeUser(), makeProject());
    renderConsole();
    expect(screen.getByTestId("home")).toBeInTheDocument();
    expect(screen.queryByTestId("console")).not.toBeInTheDocument();
  });

  it("管理员访问管理台放行", () => {
    signInAs(null, makeUser({ is_admin: true }));
    renderConsole();
    expect(screen.getByTestId("console")).toBeInTheDocument();
  });
});
