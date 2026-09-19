import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act } from "@testing-library/react";
import { toast } from "sonner";
import { useEventStream } from "./events";
import { renderWithProviders, signInAs } from "../test/render";
import { makeMember, makeProject } from "../test/fixtures";
import { useAuthStore } from "../app/store";
import { queryKeys } from "../lib/queryKeys";

vi.mock("sonner", () => ({ toast: { info: vi.fn(), warning: vi.fn() } }));

class TestEventSource {
  static instances: TestEventSource[] = [];
  listeners = new Map<string, EventListener>();
  close = vi.fn();
  constructor(public url: string) { TestEventSource.instances.push(this); }
  addEventListener(type: string, listener: EventListener) { this.listeners.set(type, listener); }
  removeEventListener(type: string) { this.listeners.delete(type); }
}

function Stream() { useEventStream(); return null; }

describe("requirement events", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    TestEventSource.instances = [];
    vi.stubGlobal("EventSource", TestEventSource);
    signInAs(makeMember({ role: "leader" }), undefined, makeProject({ id: "project-a" }));
  });
  afterEach(() => vi.unstubAllGlobals());

  it.each(["handoff.created", "handoff.accepted", "handoff.changes_requested"])("refreshes handoff inbox, targets and tasks for %s", (type) => {
    const view = renderWithProviders(<Stream />);
    const keys = [queryKeys.handoffs("received", "pending"), queryKeys.handoffs("task", "wi-2"), queryKeys.handoffTargets("wi-1"), queryKeys.workItems(), queryKeys.deliverables(), queryKeys.members()];
    keys.forEach((key) => { view.queryClient.setQueryData(key, []); });
    const otherProject = ["project-b", "handoffs"];
    view.queryClient.setQueryData(otherProject, []);
    const stream = TestEventSource.instances[0];
    expect(stream.listeners.has(type)).toBe(true);
    act(() => stream.listeners.get(type)!(new MessageEvent(type, { data: JSON.stringify({ type, data: {} }) })));
    keys.forEach((key) => expect(view.queryClient.getQueryState(key)?.isInvalidated).toBe(true));
    expect(view.queryClient.getQueryState(otherProject)?.isInvalidated).toBe(false);
  });

  it.each(["requirements.dispatched", "requirements.replied"])("invalidates only the current project and toasts %s", (type) => {
    const view = renderWithProviders(<Stream />);
    const a = queryKeys.projectRequirements();
    view.queryClient.setQueryData(a, []);
    act(() => useAuthStore.getState().setCurrentProject(makeProject({ id: "project-b" })));
    const b = queryKeys.projectRequirements();
    view.queryClient.setQueryData(b, []);
    act(() => useAuthStore.getState().setCurrentProject(makeProject({ id: "project-a" })));
    const stream = TestEventSource.instances[TestEventSource.instances.length - 1];
    expect(stream.url).toContain("project_id=project-a");
    const listener = stream.listeners.get(type)!;
    const message = new MessageEvent("message", { data: JSON.stringify({
      id: "event-1", type, created_at: "2026-09-18T00:00:00Z",
      data: { title: "项目需求更新", body: "请查看项目需求", link: "/project-requirements" },
    }) });
    act(() => listener(message));
    expect(view.queryClient.getQueryState(a)?.isInvalidated).toBe(true);
    expect(view.queryClient.getQueryState(b)?.isInvalidated).toBe(false);
    expect(toast.info).toHaveBeenCalledWith("项目需求更新", { description: "请查看项目需求" });
    act(() => useAuthStore.getState().setCurrentProject(makeProject({ id: "project-b" })));
    act(() => listener(message));
    expect(view.queryClient.getQueryState(b)?.isInvalidated).toBe(false);
    expect(toast.info).toHaveBeenCalledTimes(1);
    expect(stream.close).toHaveBeenCalled();
    view.unmount();
    expect(TestEventSource.instances[TestEventSource.instances.length - 1].listeners.size).toBe(0);
  });
});
