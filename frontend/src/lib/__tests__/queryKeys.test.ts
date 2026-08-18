import { afterEach, describe, expect, it } from "vitest";

import { useAuthStore } from "../../app/store";
import { makeProject } from "../../test/fixtures";
import { queryKeys } from "../queryKeys";

/**
 * queryKeys 项目感知键工厂（ticket 08 ③）：
 * 选定项目后业务缓存键以项目 id 打头，隔离跨项目数据；
 * 未选项目（登录分流前 / 全局管理员）不加前缀。
 */
describe("queryKeys 项目感知键工厂", () => {
  afterEach(() => {
    useAuthStore.getState().clear();
  });

  it("未选项目时不加前缀（登录分流前 / 全局管理员）", () => {
    expect(queryKeys.workItems()).toEqual(["work-items"]);
    expect(queryKeys.workItems("status=IN_REVIEW")).toEqual([
      "work-items",
      "status=IN_REVIEW",
    ]);
    expect(queryKeys.members()).toEqual(["members"]);
    expect(queryKeys.agentSuggestions("dashboard")).toEqual([
      "agent-suggestions",
      "dashboard",
    ]);
    expect(queryKeys.auditEvents()).toEqual(["audit-events"]);
  });

  it("选定项目后键以项目 id 打头，隔离跨项目缓存", () => {
    useAuthStore.getState().setCurrentProject(makeProject());

    expect(queryKeys.workItems()).toEqual(["project-1", "work-items"]);
    expect(queryKeys.workItems("wi-1")).toEqual([
      "project-1",
      "work-items",
      "wi-1",
    ]);
    expect(queryKeys.transferRequests("detail", "tr-1")).toEqual([
      "project-1",
      "transfer-requests",
      "detail",
      "tr-1",
    ]);
    expect(queryKeys.auditEvents()).toEqual(["project-1", "audit-events"]);
  });

  it("切换项目后得到全新键，与原项目键互不干扰", () => {
    const store = useAuthStore.getState();
    store.setCurrentProject(makeProject());
    const inA = queryKeys.workItems();

    store.setCurrentProject(makeProject({ id: "project-2", name: "Beta" }));
    const inB = queryKeys.workItems();

    expect(inA).toEqual(["project-1", "work-items"]);
    expect(inB).toEqual(["project-2", "work-items"]);
  });
});
