import { useEffect } from "react";
import { useQueryClient, type QueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { useAuthStore } from "../app/store";
import { queryKeys } from "../lib/queryKeys";
import type { RealtimeEvent } from "../types";

/** 后端会推送的事件类型（4.3 节，T3.6）：EventSource 对命名事件需逐一监听。 */
const EVENT_TYPES = [
  "work_item.created",
  "work_item.updated",
  "work_item.published",
  "work_item.started",
  "work_item.blocked",
  "work_item.unblocked",
  "work_item.submitted",
  "work_item.cancelled",
  "work_item.assignee_changed",
  "collaboration.requested",
  "collaboration.accepted",
  "collaboration.declined",
  "collaboration.started",
  "collaboration.submitted",
  "collaboration.revision_requested",
  "collaboration.completed",
  "collaboration.cancelled",
  "transfer.requested",
  "transfer.approved",
  "transfer.rejected",
  "transfer.cancelled",
  "deadline_change.requested",
  "deadline_change.approved",
  "deadline_change.rejected",
  "deadline_change.cancelled",
  "reminder.due_soon",
  "reminder.overdue",
  "member.created",
  "member.updated",
  "review.approved",
  "review.changes_requested",
  "review.rejected",
  // T5.7：Agent 分析完成（4.3 节），触发建议中心自动刷新
  "agent.suggestion_ready",
  // 注：deliverable.submitted / file.uploaded / file.downloaded 仅为审计 action，
  // 后端不发布对应 SSE（以 domains/deliverables、domains/files 代码为准），无需监听。
] as const;

/**
 * 按事件类型失效对应 TanStack Query 缓存（queryKey 前缀匹配，覆盖列表与详情）。
 * 业务缓存键经 queryKeys 工厂生成、已含当前项目前缀，只失效当前项目的缓存。
 */
function invalidateForEvent(queryClient: QueryClient, type: string) {
  // 任何事件都可能伴随站内通知；audit-events 为全局键（管理视角，不经工厂）
  queryClient.invalidateQueries({ queryKey: queryKeys.notifications() });
  queryClient.invalidateQueries({ queryKey: ["audit-events"] });

  const domain = type.split(".")[0];
  switch (domain) {
    case "work_item":
      queryClient.invalidateQueries({ queryKey: queryKeys.workItems() });
      break;
    case "collaboration":
      queryClient.invalidateQueries({ queryKey: queryKeys.collaborationRequests() });
      break;
    case "transfer":
      queryClient.invalidateQueries({ queryKey: queryKeys.transferRequests() });
      queryClient.invalidateQueries({ queryKey: queryKeys.approvals() });
      // 转派通过会变更主执行人
      queryClient.invalidateQueries({ queryKey: queryKeys.workItems() });
      break;
    case "deadline_change":
      queryClient.invalidateQueries({ queryKey: queryKeys.deadlineChangeRequests() });
      queryClient.invalidateQueries({ queryKey: queryKeys.approvals() });
      // DDL 变更通过会更新工作项/协作的截止时间
      queryClient.invalidateQueries({ queryKey: queryKeys.workItems() });
      queryClient.invalidateQueries({ queryKey: queryKeys.collaborationRequests() });
      break;
    case "reminder":
      queryClient.invalidateQueries({ queryKey: queryKeys.workItems() });
      queryClient.invalidateQueries({ queryKey: queryKeys.collaborationRequests() });
      break;
    case "member":
      queryClient.invalidateQueries({ queryKey: queryKeys.members() });
      break;
    case "review":
      // 审核结论推进工作项状态，并产生新的 reviews 记录与审批中心变化
      queryClient.invalidateQueries({ queryKey: queryKeys.workItems() });
      queryClient.invalidateQueries({ queryKey: queryKeys.reviews() });
      queryClient.invalidateQueries({ queryKey: queryKeys.approvals() });
      break;
    case "agent":
      // Agent 分析完成：刷新建议中心与运行记录（4.3 节，T5.7）
      queryClient.invalidateQueries({ queryKey: queryKeys.agentSuggestions() });
      queryClient.invalidateQueries({ queryKey: queryKeys.agentRuns() });
      break;
  }
}

/**
 * 项目 SSE 接入（4.3、12.6 节）：AppLayout 层建立 EventSource，
 * 收到事件后失效对应查询缓存实现自动刷新；reminder.* 弹 Sonner 提示。
 *
 * - EventSource 无法自定义请求头，token 与项目上下文走查询参数（nginx 已配流式转发）；
 * - accessToken / currentProject 变化（登录/刷新/登出/切换项目）时自动重连或关闭；
 * - 未选定项目（全局管理员 / 登录分流前）不建立连接，全局流属管理控制台（ticket 10）；
 * - 断线由浏览器 EventSource 自动重连，重连期间漏发的事件
 *   由"收到任意事件即失效相关缓存"兜底（与后端约定一致）。
 */
export function useEventStream() {
  const queryClient = useQueryClient();
  const accessToken = useAuthStore((s) => s.accessToken);
  const projectId = useAuthStore((s) => s.currentProject?.id);

  useEffect(() => {
    if (!accessToken || !projectId) {
      return;
    }
    const params = new URLSearchParams({
      token: accessToken,
      project_id: projectId,
    });
    const source = new EventSource(`/api/v1/events/stream?${params.toString()}`);

    const handle = (message: MessageEvent<string>) => {
      let event: RealtimeEvent;
      try {
        event = JSON.parse(message.data) as RealtimeEvent;
      } catch {
        return;
      }
      invalidateForEvent(queryClient, event.type);
      if (event.type.startsWith("reminder.")) {
        toast.warning(event.data.title, { description: event.data.body });
      }
      if (event.type === "agent.suggestion_ready") {
        toast.info(event.data.title, { description: event.data.body });
      }
    };

    for (const type of EVENT_TYPES) {
      source.addEventListener(type, handle as EventListener);
    }

    return () => {
      for (const type of EVENT_TYPES) {
        source.removeEventListener(type, handle as EventListener);
      }
      source.close();
    };
  }, [accessToken, projectId, queryClient]);
}
