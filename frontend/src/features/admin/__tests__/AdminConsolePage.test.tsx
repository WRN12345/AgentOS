import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route, Routes } from "react-router-dom";

vi.mock("../../../services/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../services/api")>();
  const { mockApi } = await import("../../../test/mock-api");
  return { ...actual, api: mockApi };
});

import { useAuthStore } from "../../../app/store";
import AdminConsolePage from "../AdminConsolePage";
import { mockApi, stubGet } from "../../../test/mock-api";
import { renderWithProviders, signInAs } from "../../../test/render";
import { makeAdminProject, makeUser } from "../../../test/fixtures";
import type { AuditEvent } from "../../../types";

/** 平台级审计事件夹具（GET /audit-events 形状）。 */
function makeAuditEvent(overrides: Partial<AuditEvent> = {}): AuditEvent {
  return {
    id: "audit-1",
    actor_id: "user-1",
    action: "project.created",
    target_type: "project",
    target_id: "project-1",
    before: null,
    after: { name: "Alpha" },
    request_id: "req-1",
    source_ip: null,
    created_at: "2026-01-02T00:00:00Z",
    ...overrides,
  };
}

/** 点击 shadcn Select 触发器后从 listbox 选择选项。 */
async function pickSelectOption(
  user: ReturnType<typeof userEvent.setup>,
  trigger: HTMLElement,
  optionName: string,
) {
  await user.click(trigger);
  const listbox = await screen.findByRole("listbox");
  await user.click(within(listbox).getByText(optionName));
}

/**
 * 管理控制台（ticket 10）：项目列表/新建、账号管理、审计三块。
 * 全部接口为 admin-only，管理员无项目上下文，接口不携带 X-Project-Id。
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

  it("渲染项目列表/账号管理/审计三个标签页与管理员用户名", () => {
    signInAs(null, makeUser({ is_admin: true, username: "root" }));
    stubGet({ "/projects": [], "/users": [], "/audit-events": [] });

    renderConsole();

    expect(screen.getByRole("tab", { name: "项目列表" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "账号管理" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "审计" })).toBeInTheDocument();
    expect(screen.getByText(/root/)).toBeInTheDocument();
  });

  it("项目列表展示全部项目与负责人（无负责人显示占位）", async () => {
    signInAs(null, makeUser({ is_admin: true }));
    stubGet({
      "/projects": [
        makeAdminProject(),
        makeAdminProject({ id: "project-2", name: "Beta", leader: null }),
      ],
    });

    renderConsole();

    expect(await screen.findByText("Alpha")).toBeInTheDocument();
    expect(screen.getByText("李四")).toBeInTheDocument();
    expect(screen.getByText("leader")).toBeInTheDocument();
    // Beta 无负责人：行内显示占位符，且不含负责人信息
    const betaRow = screen.getByText("Beta").closest("tr");
    expect(betaRow).not.toBeNull();
    expect(
      within(betaRow as HTMLElement).queryByText("李四"),
    ).toBeNull();
    expect(
      within(betaRow as HTMLElement).queryByText("leader"),
    ).toBeNull();
    expect(
      within(betaRow as HTMLElement).getAllByText("—").length,
    ).toBeGreaterThan(0);
  });

  it("新建项目：填名称、选负责人，提交 POST /projects 并携带幂等键", async () => {
    const user = userEvent.setup();
    const owner = makeUser({
      id: "user-owner",
      username: "leader",
      is_active: true,
      is_admin: false,
    });
    signInAs(null, makeUser({ is_admin: true }));
    stubGet({ "/projects": [], "/users": [owner] });
    mockApi.post.mockResolvedValue(makeAdminProject());

    renderConsole();

    await user.click(screen.getByRole("button", { name: "新建项目" }));
    await screen.findByRole("dialog");
    await user.type(screen.getByLabelText("项目名称"), "Gamma");
    await pickSelectOption(user, screen.getByRole("combobox"), "leader");
    await user.click(screen.getByRole("button", { name: "创建项目" }));

    await waitFor(() => {
      expect(mockApi.post).toHaveBeenCalledWith(
        "/projects",
        expect.objectContaining({
          name: "Gamma",
          owner_user_id: "user-owner",
        }),
        expect.any(String),
      );
    });
  });

  it("账号管理：展示账号列表，禁用非本人账号走 PATCH /users/{id}", async () => {
    const user = userEvent.setup();
    const me = makeUser({ id: "user-admin", username: "root", is_admin: true });
    const target = makeUser({
      id: "user-2",
      username: "bob",
      is_active: true,
      is_admin: false,
    });
    signInAs(null, me);
    stubGet({ "/projects": [], "/users": [me, target] });
    mockApi.patch.mockResolvedValue({ ...target, is_active: false });

    renderConsole();

    await user.click(screen.getByRole("tab", { name: "账号管理" }));
    expect(await screen.findByText("bob")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "禁用" }));

    await waitFor(() => {
      expect(mockApi.patch).toHaveBeenCalledWith(
        "/users/user-2",
        { is_active: false },
        expect.any(String),
      );
    });
  });

  it("当前登录管理员所在行不提供禁用按钮（避免锁死管理入口）", async () => {
    const user = userEvent.setup();
    const me = makeUser({ id: "user-admin", username: "root", is_admin: true });
    signInAs(null, me);
    stubGet({ "/projects": [], "/users": [me] });

    renderConsole();

    await user.click(screen.getByRole("tab", { name: "账号管理" }));
    await screen.findByText("root");

    const selfRow = screen.getByText("root").closest("tr");
    expect(selfRow).not.toBeNull();
    expect(
      within(selfRow as HTMLElement).queryByRole("button", { name: "禁用" }),
    ).toBeNull();
  });

  it("审计标签页展示平台级审计事件（project.created 映射为中文）", async () => {
    const user = userEvent.setup();
    signInAs(null, makeUser({ is_admin: true }));
    stubGet({ "/projects": [], "/users": [], "/audit-events": [makeAuditEvent()] });

    renderConsole();

    await user.click(screen.getByRole("tab", { name: "审计" }));
    expect(await screen.findByText("创建项目")).toBeInTheDocument();
  });

  it("点「登出」撤销令牌并回到登录页", async () => {
    const user = userEvent.setup();
    mockApi.post.mockResolvedValue({});
    signInAs(null, makeUser({ is_admin: true }));
    stubGet({ "/projects": [], "/users": [], "/audit-events": [] });

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
