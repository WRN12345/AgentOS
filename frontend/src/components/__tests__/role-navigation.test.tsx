import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, within } from "@testing-library/react";

vi.mock("../../services/events", () => ({ useEventStream: vi.fn() }));
vi.mock("../../features/notifications/NotificationBell", () => ({
  NotificationBell: () => null,
}));

import AppLayout from "../AppLayout";
import { renderWithProviders, signInAs } from "../../test/render";
import { makeLeader, makeMember } from "../../test/fixtures";

describe("角色导航", () => {
  beforeEach(() => vi.clearAllMocks());

  it("成员从任务工作，知识库提供只读项目约定和问答入口", () => {
    signInAs(makeMember());
    renderWithProviders(<AppLayout />);

    expect(screen.getByRole("link", { name: "任务" })).toHaveAttribute("href", "/work-items");
    expect(screen.getByRole("link", { name: "项目约定" })).toHaveAttribute("href", "/core-memory");
    expect(screen.getByRole("link", { name: "知识库问答" })).toHaveAttribute("href", "/knowledge-qa");
    expect(screen.queryByRole("link", { name: "AI 建议与运行" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "项目需求" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "AI 需求拆解" })).not.toBeInTheDocument();
  });

  it("负责人可以访问项目需求和 AI 管理入口", () => {
    signInAs(makeLeader());
    renderWithProviders(<AppLayout />);

    expect(screen.getByRole("link", { name: "项目需求" })).toHaveAttribute("href", "/project-requirements");
    expect(screen.getByRole("link", { name: "AI 建议与运行" })).toHaveAttribute("href", "/agent-assistant");
    expect(screen.getByRole("link", { name: "项目约定" })).toBeInTheDocument();
  });

  it.each([
    { role: "负责人", member: makeLeader(), collaboration: ["项目需求", "任务", "交付物", "审批中心"], team: ["成员与能力", "AI 建议与运行"] },
    { role: "成员", member: makeMember(), collaboration: ["任务", "交付物", "审批中心"], team: ["成员与能力"] },
  ])("$role 按四组展示菜单并保留权限和顺序", ({ member, collaboration, team }) => {
    signInAs(member);
    renderWithProviders(<AppLayout />, { route: "/work-items/task-1" });
    const nav = within(screen.getByRole("navigation"));
    expect(nav.getAllByRole("heading", { level: 2 }).map((heading) => heading.textContent))
      .toEqual(["概览", "项目协作", "团队与 AI", "知识库"]);

    for (const [name, labels] of [
      ["概览", ["工作台", "团队概览"]],
      ["项目协作", collaboration],
      ["团队与 AI", team],
      ["知识库", ["知识库文档", "项目约定", "知识库问答"]],
    ] as const) {
      const group = within(nav.getByRole("region", { name }));
      expect(group.getAllByRole("link").map((link) => link.textContent)).toEqual(labels);
    }

    expect(nav.getByRole("link", { name: "任务" })).toHaveAttribute("aria-current", "page");
    expect(nav.getAllByRole("link").filter((link) => link.hasAttribute("aria-current"))).toHaveLength(1);
    expect(nav.getByRole("link", { name: "任务" })).toHaveClass("bg-sidebar-accent");
    expect(nav.getByRole("link", { name: "工作台" })).toHaveClass("hover:bg-sidebar-accent/40");
  });
});
