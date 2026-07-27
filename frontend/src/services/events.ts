import { useEffect } from "react";
import { useQueryClient, type QueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { useAuthStore } from "../app/store";
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
] as const;

/** 按事件类型失效对应 TanStack Query 缓存（queryKey 前缀匹配，覆盖列表与详情）。 */
function invalidateForEvent(queryClient: QueryClient, type: string) {
  // 任何事件都可能伴随站内通知与审计留痕
  queryClient.invalidateQueries({ queryKey: ["notifications"] });
  queryClient.invalidateQueries({ queryKey: ["audit-events"] });

  const domain = type.split(".")[0];
  switch (domain) {
    case "work_item":
      queryClient.invalidateQueries({ queryKey: ["work-items"] });
      break;
    case "collaboration":
      queryClient.invalidateQueries({ queryKey: ["collaboration-requests"] });
      break;
    case "transfer":
      queryClient.invalidateQueries({ queryKey: ["transfer-requests"] });
      queryClient.invalidateQueries({ queryKey: ["approvals"] });
      // 转派通过会变更主执行人
      queryClient.invalidateQueries({ queryKey: ["work-items"] });
      break;
    case "deadline_change":
      queryClient.invalidateQueries({ queryKey: ["deadline-change-requests"] });
      queryClient.invalidateQueries({ queryKey: ["approvals"] });
      // DDL 变更通过会更新工作项/协作的截止时间
      queryClient.invalidateQueries({ queryKey: ["work-items"] });
      queryClient.invalidateQueries({ queryKey: ["collaboration-requests"] });
      break;
    case "reminder":
      queryClient.invalidateQueries({ queryKey: ["work-items"] });
      queryClient.invalidateQueries({ queryKey: ["collaboration-requests"] });
      break;
    case "member":
      queryClient.invalidateQueries({ queryKey: ["members"] });
      break;
  }
}

/**
 * 全局 SSE 接入（4.3、12.6 节）：AppLayout 层建立 EventSource，
 * 收到事件后失效对应查询缓存实现自动刷新；reminder.* 弹 Sonner 提示。
 *
 * - EventSource 无法自定义请求头，token 走查询参数（nginx 已配流式转发）；
 * - accessToken 变化（登录/刷新/登出）时自动重连或关闭；
 * - 断线由浏览器 EventSource 自动重连，重连期间漏发的事件
 *   由"收到任意事件即失效相关缓存"兜底（与后端约定一致）。
 */
export function useEventStream() {
  const queryClient = useQueryClient();
  const accessToken = useAuthStore((s) => s.accessToken);

  useEffect(() => {
    if (!accessToken) {
      return;
    }
    const source = new EventSource(
      `/api/v1/events/stream?token=${encodeURIComponent(accessToken)}`,
    );

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
  }, [accessToken, queryClient]);
}
