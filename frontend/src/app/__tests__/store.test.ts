import { renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import {
  useAuthStore,
  useCanManageMembers,
  useIsAdmin,
  useIsLeader,
} from "../store";
import { makeMember, makeProject, makeUser } from "../../test/fixtures";

describe("auth store 项目上下文", () => {
  it("useIsAdmin 读取 user.is_admin：is_admin=true 即管理员", () => {
    useAuthStore
      .getState()
      .setIdentity(makeUser({ is_admin: true }), makeMember());
    const { result } = renderHook(() => useIsAdmin());
    expect(result.current).toBe(true);
  });

  it("useIsAdmin 不认成员记录角色：非 admin 用户即使挂了负责人成员记录也不是管理员", () => {
    useAuthStore
      .getState()
      .setIdentity(makeUser({ is_admin: false }), makeMember({ role: "leader" }));
    const { result } = renderHook(() => useIsAdmin());
    expect(result.current).toBe(false);
  });

  it("useIsAdmin 对全局 admin 无成员记录也生效", () => {
    useAuthStore.getState().setIdentity(makeUser({ is_admin: true }), null);
    const { result } = renderHook(() => useIsAdmin());
    expect(result.current).toBe(true);
  });

  it("useIsLeader 仍以当前项目成员角色为准", () => {
    useAuthStore
      .getState()
      .setIdentity(makeUser({ is_admin: true }), makeMember({ role: "leader" }));
    const { result } = renderHook(() => useIsLeader());
    expect(result.current).toBe(true);
  });

  it("useCanManageMembers：负责人或全局 admin 均可管理成员", () => {
    useAuthStore
      .getState()
      .setIdentity(makeUser({ is_admin: false }), makeMember());
    expect(renderHook(() => useCanManageMembers()).result.current).toBe(false);

    useAuthStore.getState().setIdentity(makeUser({ is_admin: true }), null);
    expect(renderHook(() => useCanManageMembers()).result.current).toBe(true);

    useAuthStore
      .getState()
      .setIdentity(makeUser({ is_admin: false }), makeMember({ role: "leader" }));
    expect(renderHook(() => useCanManageMembers()).result.current).toBe(true);
  });

  it("setProjects 记录项目列表；setCurrentProject 置空当前成员，避免残留上一项目成员", () => {
    const project = makeProject();
    const store = useAuthStore.getState();
    store.setIdentity(makeUser(), makeMember());
    store.setProjects([project]);
    store.setCurrentProject(project);
    expect(useAuthStore.getState().projects).toEqual([project]);
    expect(useAuthStore.getState().currentProject?.id).toBe("project-1");
    expect(useAuthStore.getState().member).toBeNull();
  });

  it("clear 清空项目上下文", () => {
    const store = useAuthStore.getState();
    store.setProjects([makeProject()]);
    store.setCurrentProject(makeProject());
    store.clear();
    expect(useAuthStore.getState().projects).toBeNull();
    expect(useAuthStore.getState().currentProject).toBeNull();
  });
});
