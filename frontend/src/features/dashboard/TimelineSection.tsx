import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { api } from "../../services/api";
import type { AuditEvent, Member } from "../../types";
import { formatDateTime } from "../work-items/constants";
import { queryKeys } from "../../lib/queryKeys";
import { ACTION_LABELS, TARGET_TYPE_LABELS } from "../../lib/auditLabels";

/** 项目时间线（13.1 节，仅负责人）：GET /audit-events 关键事件流。 */
export function TimelineSection() {
  const { data: events } = useQuery({
    queryKey: queryKeys.auditEvents(),
    queryFn: () => api.get<AuditEvent[]>("/audit-events?limit=30"),
  });

  const { data: members } = useQuery({
    queryKey: queryKeys.members(),
    queryFn: () => api.get<Member[]>("/members"),
  });

  // actor_id 是用户 ID，映射到成员显示名
  const nameByUserId = new Map(
    (members ?? []).map((m) => [m.user_id, m.display_name]),
  );
  const actorName = (e: AuditEvent) =>
    e.actor_id ? (nameByUserId.get(e.actor_id) ?? "未知成员") : "系统";

  return (
    <Card>
      <CardHeader>
        <CardTitle>项目时间线</CardTitle>
        <CardDescription>最近 30 条关键事件（审计留痕）</CardDescription>
      </CardHeader>
      <CardContent>
        {!events || events.length === 0 ? (
          <p className="text-sm text-muted-foreground">暂无事件</p>
        ) : (
          <ul className="space-y-2">
            {events.map((e) => (
              <li
                key={e.id}
                className="flex flex-wrap items-center gap-2 text-sm"
              >
                <span className="shrink-0 text-muted-foreground">
                  {formatDateTime(e.created_at)}
                </span>
                <span className="font-medium">{actorName(e)}</span>
                <Badge variant="secondary">
                  {ACTION_LABELS[e.action] ?? e.action}
                </Badge>
                {e.target_type === "work_item" && e.target_id ? (
                  <Link
                    to={`/work-items/${e.target_id}`}
                    className="text-primary hover:underline"
                  >
                    {TARGET_TYPE_LABELS[e.target_type]}
                  </Link>
                ) : e.target_type ? (
                  <span className="text-muted-foreground">
                    {TARGET_TYPE_LABELS[e.target_type] ?? e.target_type}
                  </span>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
