import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));
vi.mock("../services/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../services/api")>();
  const { mockApi } = await import("../test/mock-api");
  return { ...actual, api: mockApi };
});

import AdminConsolePage from "../features/admin/AdminConsolePage";
import { mockApi } from "../test/mock-api";
import { renderWithProviders, signInAs } from "../test/render";
import { makeUser } from "../test/fixtures";
import { queryKeys } from "../lib/queryKeys";

const admin = makeUser({ id: "admin-id", username: "admin", is_admin: true });
const owner = makeUser({ id: "owner-id", username: "owner" });
const projects = [
  {
    id: "project-a",
    name: "客户服务平台",
    description: "客户服务项目",
    leader: {
      id: "member-a",
      user_id: owner.id,
      username: owner.username,
      display_name: "负责人甲",
    },
    created_at: "2026-09-01T00:00:00Z",
    updated_at: "2026-09-17T00:00:00Z",
    total: 10,
    completed: 6,
    active: 4,
    overdue: 1,
    blocked: 1,
  },
  {
    id: "project-b",
    name: "空项目",
    description: null,
    leader: null,
    created_at: "2026-09-01T00:00:00Z",
    updated_at: "2026-09-17T00:00:00Z",
    total: 0,
    completed: 0,
    active: 0,
    overdue: 0,
    blocked: 0,
  },
];
const members = [
  {
    member_id: "member-a",
    user_id: owner.id,
    project_id: "project-a",
    username: owner.username,
    display_name: "负责人甲",
    role: "leader",
    is_active: true,
    user_is_active: true,
    active: 3,
    completed_total: 6,
    completed_recent: 4,
    overdue: 1,
    blocked: 1,
    on_time_rate: 5 / 6,
    sample_sufficient: true,
  },
  {
    member_id: "member-disabled",
    user_id: "disabled-id",
    project_id: "project-a",
    username: "disabled",
    display_name: "停用成员乙",
    role: "member",
    is_active: false,
    user_is_active: false,
    active: 1,
    completed_total: 0,
    completed_recent: 0,
    overdue: 0,
    blocked: 0,
    on_time_rate: null,
    sample_sufficient: false,
  },
];
const attentionItem = {
  id: "task-a",
  project_id: "project-a",
  title: "等待接口联调",
  status: "BLOCKED",
  priority: "high",
  assignee_id: "member-a",
  assignee_name: "负责人甲",
  due_at: "2026-09-16T00:00:00Z",
  updated_at: "2026-09-17T00:00:00Z",
  is_overdue: true,
};
const auditEvent = {
  id: "event-a",
  project_id: "project-a",
  actor_id: admin.id,
  action: "project.created",
  target_type: "project",
  target_id: "project-a",
  before: null,
  after: { name: "客户服务平台" },
  request_id: null,
  source_ip: null,
  created_at: "2026-09-17T00:00:00Z",
};

function stubAdminData() {
  mockApi.get.mockImplementation((path: string) => {
    const url = new URL(path, "http://localhost");
    switch (url.pathname) {
      case "/admin/overview":
        return Promise.resolve({
          as_of: "2026-09-17T00:00:00Z",
          projects,
          members,
        });
      case "/admin/attention":
        return Promise.resolve({
          items:
            url.searchParams.get("project_id") === "project-b"
              ? []
              : [attentionItem],
          total: url.searchParams.get("project_id") === "project-b" ? 0 : 1,
        });
      case "/projects":
        return Promise.resolve(projects);
      case "/users":
        return Promise.resolve([admin, owner]);
      case "/audit-events":
        return Promise.resolve(
          url.searchParams.get("project_id") === "project-b" ||
            url.searchParams.get("platform_only") === "true"
            ? []
            : [auditEvent],
        );
      default:
        return Promise.reject(new Error(`Unexpected API request: ${path}`));
    }
  });
}

async function goTo(label: string) {
  await userEvent.click(
    within(screen.getByRole("navigation", { name: "管理员导航" })).getByText(
      label,
    ),
  );
}

describe("管理员领导视角控制台", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    signInAs(null, admin);
    stubAdminData();
  });

  it("默认展示真实项目进度，创建项目只在项目管理入口，侧栏无折叠按钮", async () => {
    renderWithProviders(<AdminConsolePage />, { route: "/console" });
    expect(await screen.findByText("60%")).toBeInTheDocument();
    expect(screen.getAllByText("暂无任务").length).toBeGreaterThan(0);
    const nav = screen.getByRole("navigation", { name: "管理员导航" });
    for (const label of [
      "管理总览",
      "项目管理",
      "人员工作",
      "账号管理",
      "系统审计",
    ]) {
      expect(within(nav).getByText(label)).toBeInTheDocument();
    }
    expect(
      screen.queryByRole("button", { name: /新建项目/ }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /侧边栏/ }),
    ).not.toBeInTheDocument();
    await goTo("项目管理");
    await userEvent.click(screen.getByRole("button", { name: /新建项目/ }));
    expect(
      await screen.findByRole("dialog", { name: "新建项目" }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("项目名称")).toBeInTheDocument();
    expect(mockApi.post).not.toHaveBeenCalled();
  });

  it("项目下钻使用受控项目筛选，不访问成员业务接口，任务摘要只读", async () => {
    renderWithProviders(<AdminConsolePage />);
    await screen.findByText("60%");
    await userEvent.click(
      screen.getAllByRole("button", { name: /查看进度/ })[0],
    );
    await waitFor(() => {
      const urls = mockApi.get.mock.calls.map(
        ([path]) => new URL(String(path), "http://localhost"),
      );
      expect(
        urls.some(
          (u) =>
            u.pathname === "/admin/attention" &&
            u.searchParams.get("project_id") === "project-a",
        ),
      ).toBe(true);
      expect(
        urls.some(
          (u) =>
            u.pathname === "/audit-events" &&
            u.searchParams.get("project_id") === "project-a",
        ),
      ).toBe(true);
    });
    await userEvent.click(
      await screen.findByRole("button", { name: /等待接口联调/ }),
    );
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("等待接口联调")).toBeInTheDocument();
    expect(
      within(dialog).queryByRole("button", { name: /审批|分配|提交/ }),
    ).not.toBeInTheDocument();
    expect(
      mockApi.get.mock.calls.some(([path]) =>
        /^\/(members|work-items)(\/|\?|$)/.test(String(path)),
      ),
    ).toBe(false);
  });

  it("人员视图保留停用成员与小样本提示", async () => {
    renderWithProviders(<AdminConsolePage />);
    await goTo("人员工作");
    expect(await screen.findByText("停用成员乙")).toBeInTheDocument();
    expect(screen.getAllByText(/样本不足/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/停用|禁用/).length).toBeGreaterThan(0);
  });

  it("新建项目复用真实接口并刷新项目总览与审计缓存", async () => {
    mockApi.post.mockResolvedValue({
      ...projects[0],
      id: "created-project",
      name: "新项目",
    });
    const { queryClient } = renderWithProviders(<AdminConsolePage />);
    const invalidate = vi.spyOn(queryClient, "invalidateQueries");
    await goTo("项目管理");
    await userEvent.click(screen.getByRole("button", { name: /新建项目/ }));
    const dialog = await screen.findByRole("dialog", { name: "新建项目" });
    await userEvent.type(within(dialog).getByLabelText("项目名称"), "新项目");
    await userEvent.type(
      within(dialog).getByLabelText("负责人用户名"),
      "owner",
    );
    await userEvent.click(
      within(dialog).getByRole("button", { name: "创建项目" }),
    );
    await waitFor(() =>
      expect(mockApi.post).toHaveBeenCalledWith(
        "/projects",
        {
          name: "新项目",
          description: null,
          owner_user_id: owner.id,
        },
        expect.any(String),
      ),
    );
    await waitFor(() =>
      expect(invalidate).toHaveBeenCalledWith({
        queryKey: queryKeys.adminOverview(),
      }),
    );
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: queryKeys.adminAuditEvents(),
    });
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
  });

  it("变更负责人保留真实写接口并刷新统计", async () => {
    mockApi.put.mockResolvedValue(projects[1]);
    const { queryClient } = renderWithProviders(<AdminConsolePage />);
    const invalidate = vi.spyOn(queryClient, "invalidateQueries");
    await goTo("项目管理");
    const buttons = await screen.findAllByRole("button", {
      name: /变更负责人/,
    });
    await userEvent.click(buttons[1]);
    const dialog = await screen.findByRole("dialog");
    await userEvent.type(
      within(dialog).getByLabelText("新负责人用户名"),
      "owner",
    );
    await userEvent.click(
      within(dialog).getByRole("button", { name: "变更负责人" }),
    );
    await waitFor(() =>
      expect(mockApi.put).toHaveBeenCalledWith(
        "/projects/project-b/leader",
        { user_id: owner.id },
        expect.any(String),
      ),
    );
    await waitFor(() =>
      expect(invalidate).toHaveBeenCalledWith({
        queryKey: queryKeys.adminOverview(),
      }),
    );
  });

  it("审计范围通过服务端过滤，项目与平台筛选互斥", async () => {
    renderWithProviders(<AdminConsolePage />);
    await goTo("系统审计");
    await screen.findByText("创建项目");
    await userEvent.selectOptions(
      screen.getByLabelText("审计范围"),
      "project-b",
    );
    await waitFor(() =>
      expect(
        mockApi.get.mock.calls.some(([path]) => {
          const url = new URL(String(path), "http://localhost");
          return (
            url.pathname === "/audit-events" &&
            url.searchParams.get("project_id") === "project-b"
          );
        }),
      ).toBe(true),
    );
    const scope = screen.getByLabelText("审计范围");
    const platformOption = within(scope).getByRole("option", {
      name: "平台管理",
    });
    await userEvent.selectOptions(scope, platformOption);
    await waitFor(() =>
      expect(
        mockApi.get.mock.calls.some(([path]) => {
          const url = new URL(String(path), "http://localhost");
          return (
            url.pathname === "/audit-events" &&
            url.searchParams.get("platform_only") === "true" &&
            !url.searchParams.has("project_id")
          );
        }),
      ).toBe(true),
    );
  });

  it("账号管理保留建号及启停，当前管理员不能禁用自己", async () => {
    mockApi.patch.mockResolvedValue({ ...owner, is_active: false });
    renderWithProviders(<AdminConsolePage />);
    await goTo("账号管理");
    await screen.findByText("owner");
    expect(screen.getAllByRole("button", { name: "禁用" })).toHaveLength(1);
    await userEvent.click(screen.getByRole("button", { name: "禁用" }));
    await waitFor(() =>
      expect(mockApi.patch).toHaveBeenCalledWith(
        `/users/${owner.id}`,
        { is_active: false },
        expect.any(String),
      ),
    );
    await userEvent.click(screen.getByRole("button", { name: /新建账号/ }));
    expect(
      await screen.findByRole("dialog", { name: "新建账号" }),
    ).toBeInTheDocument();
  });

  it("汇总加载失败显示错误而非零风险，管理功能仍可进入", async () => {
    const original = mockApi.get.getMockImplementation()!;
    mockApi.get.mockImplementation((path: string) =>
      path === "/admin/overview"
        ? Promise.reject(new Error("统计服务不可用"))
        : original(path),
    );
    renderWithProviders(<AdminConsolePage />);
    expect((await screen.findAllByRole("alert")).length).toBeGreaterThan(0);
    expect(screen.queryByText("60%")).not.toBeInTheDocument();
    await goTo("账号管理");
    expect(await screen.findByText("owner")).toBeInTheDocument();
  });

  it("切换项目不会混入前一项目的关注任务与审计", async () => {
    renderWithProviders(<AdminConsolePage />);
    await screen.findByText("60%");
    await userEvent.click(
      screen.getAllByRole("button", { name: /查看进度/ })[0],
    );
    expect(
      await screen.findByRole("button", { name: /等待接口联调/ }),
    ).toBeInTheDocument();
    await goTo("项目管理");
    await userEvent.click(
      (await screen.findAllByRole("button", { name: /查看进度/ }))[1],
    );
    await waitFor(() => {
      const urls = mockApi.get.mock.calls.map(
        ([path]) => new URL(String(path), "http://localhost"),
      );
      expect(
        urls.some(
          (u) =>
            u.pathname === "/audit-events" &&
            u.searchParams.get("project_id") === "project-b",
        ),
      ).toBe(true);
    });
    expect(
      screen.queryByRole("button", { name: /等待接口联调/ }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("创建项目")).not.toBeInTheDocument();
  });

  it("刷新失败时保留数据但显式提示错误", async () => {
    const { queryClient } = renderWithProviders(<AdminConsolePage />);
    await screen.findByText("60%");
    const original = mockApi.get.getMockImplementation()!;
    mockApi.get.mockImplementation((path: string) =>
      path === "/admin/overview"
        ? Promise.reject(new Error("暂时不可用"))
        : original(path),
    );
    await queryClient.invalidateQueries({
      queryKey: queryKeys.adminOverview(),
    });
    expect((await screen.findAllByRole("alert")).length).toBeGreaterThan(0);
    expect(screen.getByText("60%")).toBeInTheDocument();
  });

  it("审计翻页后搜索重置页码，在服务端检索并保留项目范围", async () => {
    const original = mockApi.get.getMockImplementation()!;
    mockApi.get.mockImplementation((path: string) => {
      const url = new URL(path, "http://localhost");
      if (url.pathname !== "/audit-events") return original(path);
      if (url.searchParams.get("q"))
        return Promise.resolve([{ ...auditEvent, action: "user.created" }]);
      return Promise.resolve(
        Array.from({ length: 50 }, (_, i) => ({
          ...auditEvent,
          id: `event-${url.searchParams.get("offset")}-${i}`,
        })),
      );
    });
    renderWithProviders(<AdminConsolePage />, {
      route: "/console?tab=audit&scope=project-a",
    });
    await screen.findAllByText("创建项目");
    await userEvent.click(screen.getByRole("button", { name: "下一页" }));
    await waitFor(() =>
      expect(
        mockApi.get.mock.calls.some(([path]) => {
          const url = new URL(String(path), "http://localhost");
          return (
            url.pathname === "/audit-events" &&
            url.searchParams.get("offset") === "50"
          );
        }),
      ).toBe(true),
    );
    await userEvent.type(screen.getByLabelText("搜索审计记录"), "创建账号");
    await waitFor(() =>
      expect(
        mockApi.get.mock.calls.some(([path]) => {
          const url = new URL(String(path), "http://localhost");
          return (
            url.pathname === "/audit-events" &&
            url.searchParams.get("q") === "user.created" &&
            url.searchParams.get("offset") === "0" &&
            url.searchParams.get("project_id") === "project-a"
          );
        }),
      ).toBe(true),
    );
    expect(await screen.findByText("创建账号")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "上一页" })).toBeDisabled();
  });

  it("关注任务可独立翻页，进入项目时从第一页重新查询", async () => {
    const original = mockApi.get.getMockImplementation()!;
    mockApi.get.mockImplementation((path: string) => {
      const url = new URL(path, "http://localhost");
      if (url.pathname !== "/admin/attention") return original(path);
      return Promise.resolve({
        items: [
          {
            ...attentionItem,
            title:
              url.searchParams.get("offset") === "50"
                ? "第二页关注任务"
                : attentionItem.title,
          },
        ],
        total: 51,
      });
    });
    renderWithProviders(<AdminConsolePage />);
    const firstTask = await screen.findByRole("button", {
      name: /等待接口联调/,
    });
    const card = firstTask.closest('[data-slot="card"]') as HTMLElement;
    await userEvent.click(within(card).getByRole("button", { name: "下一页" }));
    expect(
      await screen.findByRole("button", { name: /第二页关注任务/ }),
    ).toBeInTheDocument();
    await userEvent.click(
      screen.getAllByRole("button", { name: /查看进度/ })[0],
    );
    expect(
      await screen.findByRole("button", { name: /等待接口联调/ }),
    ).toBeInTheDocument();
    await waitFor(() =>
      expect(
        mockApi.get.mock.calls.some(([path]) => {
          const url = new URL(String(path), "http://localhost");
          return (
            url.pathname === "/admin/attention" &&
            url.searchParams.get("project_id") === "project-a" &&
            url.searchParams.get("offset") === "0"
          );
        }),
      ).toBe(true),
    );
  });

  it("关注任务失败不影响项目统计，也不伪装为没有风险", async () => {
    const original = mockApi.get.getMockImplementation()!;
    mockApi.get.mockImplementation((path: string) =>
      path.startsWith("/admin/attention")
        ? Promise.reject(new Error("关注任务暂不可用"))
        : original(path),
    );
    renderWithProviders(<AdminConsolePage />);
    expect(await screen.findByText("60%")).toBeInTheDocument();
    expect(await screen.findByRole("alert")).toHaveTextContent("关注任务");
    expect(screen.queryByText("本页暂无关注任务")).not.toBeInTheDocument();
  });

  it("刷新关注任务时更新打开的摘要，移出关注列表后关闭摘要", async () => {
    const { queryClient } = renderWithProviders(<AdminConsolePage />);
    await userEvent.click(
      await screen.findByRole("button", { name: /等待接口联调/ }),
    );
    expect(
      within(await screen.findByRole("dialog")).getByText("阻塞"),
    ).toBeInTheDocument();
    const original = mockApi.get.getMockImplementation()!;
    mockApi.get.mockImplementation((path: string) =>
      path.startsWith("/admin/attention")
        ? Promise.resolve({
            items: [
              {
                ...attentionItem,
                status: "IN_PROGRESS",
                assignee_name: "新执行人",
              },
            ],
            total: 1,
          })
        : original(path),
    );
    await queryClient.invalidateQueries({
      queryKey: queryKeys.adminAttention(),
    });
    expect(
      await within(screen.getByRole("dialog")).findByText("新执行人"),
    ).toBeInTheDocument();
    expect(
      within(screen.getByRole("dialog")).queryByText("阻塞"),
    ).not.toBeInTheDocument();
    mockApi.get.mockImplementation((path: string) =>
      path.startsWith("/admin/attention")
        ? Promise.resolve({ items: [], total: 0 })
        : original(path),
    );
    await queryClient.invalidateQueries({
      queryKey: queryKeys.adminAttention(),
    });
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
  });
});
