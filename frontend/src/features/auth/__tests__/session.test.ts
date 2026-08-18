import { afterEach, describe, expect, it, vi } from "vitest";
import { useAuthStore } from "../../../app/store";
import { makeMember, makeProject, makeUser } from "../../../test/fixtures";
import { api } from "../../../services/api";
import { queryClient } from "../../../app/queryClient";
import {
  PROJECT_REMEMBER_MS,
  isProjectRemembered,
  pickRememberedProject,
  selectProject,
  logout,
} from "../session";

afterEach(() => {
  vi.restoreAllMocks();
  useAuthStore.getState().clear();
});

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

describe("session 项目切换原子性", () => {
  it("加载新项目身份失败时恢复原项目和成员身份", async () => {
    const previous = makeProject({ id: "project-old" });
    const previousMember = makeMember({ id: "member-old" });
    useAuthStore.getState().setCurrentProject(previous);
    useAuthStore.getState().setIdentity(makeUser(), previousMember);

    vi.spyOn(api, "get").mockRejectedValueOnce(new Error("403"));

    await expect(
      selectProject(makeProject({ id: "project-new" })),
    ).rejects.toThrow("403");
    expect(useAuthStore.getState().currentProject).toEqual(previous);
    expect(useAuthStore.getState().member).toEqual(previousMember);
  });
});

describe("session 登出隔离", () => {
  it("登出时清空 React Query 缓存", async () => {
    useAuthStore.getState().setTokens({
      access_token: "access",
      refresh_token: "refresh",
      token_type: "bearer",
      expires_in: 1800,
    });
    queryClient.setQueryData(["project-1", "work-items"], [{ id: "secret" }]);
    vi.spyOn(api, "post").mockResolvedValueOnce({ status: "ok" });

    await logout();

    expect(queryClient.getQueryCache().getAll()).toHaveLength(0);
  });
});
