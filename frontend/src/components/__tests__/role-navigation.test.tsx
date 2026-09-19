import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";

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
});
