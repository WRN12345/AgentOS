import { useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bell } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";
import { api } from "../../services/api";
import type { AppNotification, NotificationList } from "../../types";
import { formatDateTime } from "../work-items/constants";

/** 顶栏通知入口（12.6 节）：未读数徽标 + 下拉列表，点击已读并跳转关联页面。 */
export function NotificationBell() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const { data } = useQuery({
    queryKey: ["notifications"],
    queryFn: () => api.get<NotificationList>("/notifications?limit=20"),
  });

  // 已读接口幂等：重复点击不报错
  const markRead = useMutation({
    mutationFn: (notification: AppNotification) =>
      api.post(`/notifications/${notification.id}/read`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["notifications"] });
    },
  });

  const handleClick = (notification: AppNotification) => {
    if (!notification.is_read) {
      markRead.mutate(notification);
    }
    if (notification.link) {
      navigate(notification.link);
    }
  };

  const unreadCount = data?.unread_count ?? 0;

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="icon" className="relative">
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
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-96">
        <DropdownMenuLabel className="flex items-center justify-between">
          <span>通知</span>
          {unreadCount > 0 && (
            <span className="text-xs font-normal text-muted-foreground">
              {unreadCount} 条未读
            </span>
          )}
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        <div className="max-h-96 overflow-y-auto">
          {!data || data.items.length === 0 ? (
            <p className="px-2 py-6 text-center text-sm text-muted-foreground">
              暂无通知
            </p>
          ) : (
            data.items.map((n) => (
              <DropdownMenuItem
                key={n.id}
                className="flex cursor-pointer flex-col items-start gap-1 py-2"
                onClick={() => handleClick(n)}
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
                      "flex-1 truncate text-sm",
                      !n.is_read && "font-medium",
                    )}
                  >
                    {n.title}
                  </span>
                  <span className="shrink-0 text-xs text-muted-foreground">
                    {formatDateTime(n.created_at)}
                  </span>
                </div>
                <p className="line-clamp-2 w-full whitespace-normal pl-4 text-xs text-muted-foreground">
                  {n.body}
                </p>
              </DropdownMenuItem>
            ))
          )}
        </div>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
