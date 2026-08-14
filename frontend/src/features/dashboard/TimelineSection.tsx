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

/** 审计动作中文映射（对应后端各域写入的 action 名称）。 */
const ACTION_LABELS: Record<string, string> = {
  "work_item.created": "创建工作项",
  "work_item.updated": "更新工作项",
  "work_item.published": "发布工作项",
  "work_item.started": "开始工作项",
  "work_item.blocked": "标记阻塞",
  "work_item.unblocked": "解除阻塞",
  "work_item.submitted": "提交审核",
  "work_item.cancelled": "取消工作项",
  "work_item.assignee_changed": "变更主执行人",
  "collaboration.requested": "发起协作请求",
  "collaboration.accepted": "接受协作请求",
  "collaboration.declined": "拒绝协作请求",
  "collaboration.started": "开始处理协作",
  "collaboration.submitted": "回传协作产物",
  "collaboration.revision_requested": "要求修改协作产物",
  "collaboration.completed": "完成协作请求",
  "collaboration.cancelled": "取消协作请求",
  "transfer.requested": "申请转派",
  "transfer.approved": "通过转派",
  "transfer.rejected": "驳回转派",
  "transfer.cancelled": "取消转派申请",
  "deadline_change.requested": "申请 DDL 变更",
  "deadline_change.approved": "通过 DDL 变更",
  "deadline_change.rejected": "驳回 DDL 变更",
  "deadline_change.cancelled": "取消 DDL 变更申请",
  "member.created": "新增成员",
  "member.updated": "更新成员",
  "member.capabilities.submitted": "提交能力标签",
  "member.capabilities.confirmed": "确认能力标签",
};

const TARGET_TYPE_LABELS: Record<string, string> = {
  work_item: "工作项",
  collaboration_request: "协作请求",
  transfer_request: "转派申请",
  deadline_change_request: "DDL 变更",
  project_member: "成员",
};

/** 项目时间线（13.1 节，仅负责人）：GET /audit-events 关键事件流。 */
export function TimelineSection() {
  const { data: events } = useQuery({
    queryKey: ["audit-events"],
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
