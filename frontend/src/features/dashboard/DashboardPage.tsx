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
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { api } from "../../services/api";
import type {
  Member,
  WorkItemStatus,
  WorkItemSummary,
} from "../../types";
import {
  STATUS_META,
  formatDate,
} from "../work-items/constants";

const STATUS_ORDER: WorkItemStatus[] = [
  "DRAFT",
  "READY",
  "IN_PROGRESS",
  "BLOCKED",
  "IN_REVIEW",
  "COMPLETED",
  "CANCELLED",
];

/** 距今天数（按截止时间计）。 */
function daysUntil(dueAt: string): number {
  const ms = new Date(dueAt).getTime() - Date.now();
  return Math.ceil(ms / (24 * 60 * 60 * 1000));
}

/** 团队透明看板（13.2 节）：由 GET /members 与 GET /work-items 前端聚合。 */
export default function DashboardPage() {
  const { data: members, isLoading: membersLoading } = useQuery({
    queryKey: ["members"],
    queryFn: () => api.get<Member[]>("/members"),
  });
  const { data: items, isLoading: itemsLoading } = useQuery({
    queryKey: ["work-items", ""],
    queryFn: () => api.get<WorkItemSummary[]>("/work-items"),
  });

  if (membersLoading || itemsLoading) {
    return (
      <div className="space-y-2">
        <Skeleton className="h-24 w-full" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  const all = items ?? [];

  // 状态分布
  const statusCount = new Map<WorkItemStatus, number>();
  for (const item of all) {
    statusCount.set(item.status, (statusCount.get(item.status) ?? 0) + 1);
  }

  // 即将到期：未完成且 7 天内截止
  const activeStatuses: WorkItemStatus[] = [
    "READY",
    "IN_PROGRESS",
    "BLOCKED",
    "IN_REVIEW",
  ];
  const upcoming = all
    .filter(
      (i) =>
        i.due_at &&
        activeStatuses.includes(i.status) &&
        daysUntil(i.due_at) <= 7,
    )
    .sort((a, b) => (a.due_at! < b.due_at! ? -1 : 1));

  return (
    <div className="space-y-4">
      {/* 状态分布 */}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4 lg:grid-cols-7">
        {STATUS_ORDER.map((s) => (
          <Card key={s}>
            <CardHeader className="p-4 pb-2">
              <CardDescription>{STATUS_META[s].label}</CardDescription>
              <CardTitle className="text-2xl">
                {statusCount.get(s) ?? 0}
              </CardTitle>
            </CardHeader>
          </Card>
        ))}
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        {/* 全员工作量 */}
        <Card>
          <CardHeader>
            <CardTitle>全员工作量</CardTitle>
            <CardDescription>每名成员当前的活跃任务数</CardDescription>
          </CardHeader>
          <CardContent>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>成员</TableHead>
                  <TableHead>角色</TableHead>
                  <TableHead className="text-right">活跃任务</TableHead>
                  <TableHead className="text-right">每周可投入</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {(members ?? [])
                  .filter((m) => m.is_active)
                  .map((m) => (
                    <TableRow key={m.id}>
                      <TableCell className="font-medium">
                        {m.display_name}
                      </TableCell>
                      <TableCell>
                        <Badge
                          variant={
                            m.role === "leader" ? "default" : "secondary"
                          }
                        >
                          {m.role === "leader" ? "负责人" : "成员"}
                        </Badge>
                      </TableCell>
                      <TableCell className="text-right">
                        {m.active_work_items}
                      </TableCell>
                      <TableCell className="text-right">
                        {m.weekly_available_hours != null
                          ? `${m.weekly_available_hours}h`
                          : "—"}
                      </TableCell>
                    </TableRow>
                  ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>

        {/* 即将到期 */}
        <Card>
          <CardHeader>
            <CardTitle>即将到期</CardTitle>
            <CardDescription>
              未完成且 7 天内截止的工作项（含已逾期）
            </CardDescription>
          </CardHeader>
          <CardContent>
            {upcoming.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                暂无即将到期的工作项
              </p>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>标题</TableHead>
                    <TableHead>状态</TableHead>
                    <TableHead>主执行人</TableHead>
                    <TableHead>截止时间</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {upcoming.map((i) => {
                    const overdue = daysUntil(i.due_at!) < 0;
                    return (
                      <TableRow key={i.id}>
                        <TableCell>
                          <Link
                            to={`/work-items/${i.id}`}
                            className="font-medium text-primary hover:underline"
                          >
                            {i.title}
                          </Link>
                        </TableCell>
                        <TableCell>
                          <Badge className={STATUS_META[i.status].className}>
                            {STATUS_META[i.status].label}
                          </Badge>
                        </TableCell>
                        <TableCell>{i.assignee.display_name}</TableCell>
                        <TableCell>
                          <span
                            className={
                              overdue ? "font-medium text-destructive" : ""
                            }
                          >
                            {formatDate(i.due_at)}
                            {overdue && "（已逾期）"}
                          </span>
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
