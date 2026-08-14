import { useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bell } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { SimpleMenu, simpleMenuItemClass } from "@/components/SimpleMenu";
import { cn } from "@/lib/utils";
import { api } from "../../services/api";
import type { AppNotification, NotificationList } from "../../types";
import { formatDateTime } from "../work-items/constants";
import { queryKeys } from "../../lib/queryKeys";

/** 顶栏通知入口（12.6 节）：未读数徽标 + 下拉列表，点击已读并跳转关联页面。 */
export function NotificationBell() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const { data } = useQuery({
    queryKey: queryKeys.notifications(),
    queryFn: () => api.get<NotificationList>("/notifications?limit=20"),
  });

  // 已读接口幂等：重复点击不报错
  const markRead = useMutation({
    mutationFn: (notification: AppNotification) =>
      api.post(`/notifications/${notification.id}/read`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.notifications() });
    },
  });

  const unreadCount = data?.unread_count ?? 0;

  return (
    // 轻量下拉（不用 Radix）：部分环境下 Radix 触发器无响应，见 SimpleMenu 注释
    <SimpleMenu
      contentClassName="w-96"
      trigger={(toggle, open) => (
        <Button
          variant="ghost"
          size="icon"
          className="relative"
          onClick={toggle}
          aria-expanded={open}
        >
          <Bell className="size-4" />
          {unreadCount > 0 && (
            <Badge
              variant="destructive"
              className="absolute -right-1 -top-1 h-5 min-w-5 justify-center px-1 text-xs"
            >
              {unreadCount > 99 ? "99+" : unreadCount}
            </Badge>
          )}
          <span className="sr-only">通知</span>
        </Button>
      )}
    >
      {(close) => (
        <>
          <div className="flex items-center justify-between px-1.5 py-1.5 text-sm font-medium">
            <span>通知</span>
            {unreadCount > 0 && (
              <span className="text-xs font-normal text-muted-foreground">
                {unreadCount} 条未读
              </span>
            )}
          </div>
          <div className="-mx-1 my-1 h-px bg-border" />
          <div className="max-h-96 overflow-y-auto">
            {!data || data.items.length === 0 ? (
              <p className="px-2 py-6 text-center text-sm text-muted-foreground">
                暂无通知
              </p>
            ) : (
              data.items.map((n) => (
                <button
                  type="button"
                  key={n.id}
                  className={cn(simpleMenuItemClass, "flex-col items-start gap-1 py-2")}
                  onClick={() => {
                    if (!n.is_read) {
                      markRead.mutate(n);
                    }
                    close();
                    if (n.link) {
                      navigate(n.link);
                    }
                  }}
                >
                  <div className="flex w-full items-center gap-2">
                    <span
                      className={cn(
                        "size-2 shrink-0 rounded-full",
                        n.is_read ? "bg-transparent" : "bg-blue-500",
                      )}
                    />
                    <span
                      className={cn(
                        "flex-1 truncate text-left text-sm",
                        !n.is_read && "font-medium",
                      )}
                    >
                      {n.title}
                    </span>
                    <span className="shrink-0 text-xs text-muted-foreground">
                      {formatDateTime(n.created_at)}
                    </span>
                  </div>
                  <p className="line-clamp-2 w-full whitespace-normal pl-4 text-left text-xs text-muted-foreground">
                    {n.body}
                  </p>
                </button>
              ))
            )}
          </div>
        </>
      )}
    </SimpleMenu>
  );
}
