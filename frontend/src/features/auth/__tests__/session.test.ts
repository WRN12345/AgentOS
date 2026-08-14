import { describe, expect, it } from "vitest";
import { makeProject } from "../../../test/fixtures";
import {
  PROJECT_REMEMBER_MS,
  isProjectRemembered,
  pickRememberedProject,
} from "../session";

/** 24h 记忆窗口：登录时据此决定直接进入上次项目还是进选择页（ticket 09）。 */
describe("session 项目记忆窗口", () => {
  it("isProjectRemembered：有 24h 内的时间戳才记为可直入", () => {
    expect(isProjectRemembered(null)).toBe(false);
    // 刚刚选定（当前时刻）在窗口内
    expect(isProjectRemembered(Date.now())).toBe(true);
    // 24h 整界外（25h 前）过期
    expect(isProjectRemembered(Date.now() - PROJECT_REMEMBER_MS - 1)).toBe(false);
    // 窗口内（23h 前）仍有效
    expect(isProjectRemembered(Date.now() - PROJECT_REMEMBER_MS + 1)).toBe(true);
  });

  it("pickRememberedProject：无上次项目时返回 null", () => {
    const projects = [makeProject()];
    expect(pickRememberedProject(projects, null, Date.now())).toBeNull();
  });

  it("pickRememberedProject：上次项目已过期（超过 24h）时不自动进入", () => {
    const project = makeProject();
    const projects = [project];
    expect(
      pickRememberedProject(
        projects,
        project,
        Date.now() - PROJECT_REMEMBER_MS - 1000,
      ),
    ).toBeNull();
  });

  it("pickRememberedProject：上次项目已不在我参与列表（被移出/项目删除）时不自动进入", () => {
    const remembered = makeProject({ id: "project-gone" });
    const projects = [makeProject({ id: "project-other" })];
    expect(pickRememberedProject(projects, remembered, Date.now())).toBeNull();
  });

  it("pickRememberedProject：24h 内且仍在列表时返回该项目", () => {
    const project = makeProject();
    const projects = [makeProject({ id: "project-other" }), project];
    expect(pickRememberedProject(projects, project, Date.now())).toEqual(
      project,
    );
  });
});
