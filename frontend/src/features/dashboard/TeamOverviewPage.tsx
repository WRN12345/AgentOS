import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  Bar,
  BarChart,
  Cell,
  Legend,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
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
import type { Member, WorkItemStatus, WorkItemSummary } from "../../types";
import { STATUS_META, formatDate } from "../work-items/constants";
import { useIsAdmin, useIsLeader } from "../../app/store";
import { roleBadgeVariant, roleLabel } from "../../lib/roles";
import { TimelineSection } from "./TimelineSection";
import { ACTIVE_STATUSES, daysUntil } from "./shared";
import { queryKeys } from "../../lib/queryKeys";

const STATUS_ORDER: WorkItemStatus[] = [
  "DRAFT",
  "READY",
  "IN_PROGRESS",
  "BLOCKED",
  "IN_REVIEW",
  "COMPLETED",
  "CANCELLED",
];

/** 图表配色：沿用 STATUS_META 的 Tailwind 颜色语义（recharts 需要具体色值）。 */
const STATUS_CHART_COLORS: Record<WorkItemStatus, string> = {
  DRAFT: "#9ca3af", // gray-400
  READY: "#3b82f6", // blue-500
  IN_PROGRESS: "#f59e0b", // amber-500
  BLOCKED: "#ef4444", // red-500
  IN_REVIEW: "#a855f7", // purple-500
  COMPLETED: "#22c55e", // green-500
  CANCELLED: "#d1d5db", // gray-300
};

/** 团队概览：状态分布与成员负载图表 + 工作量/到期表 + 项目时间线。 */
export default function TeamOverviewPage() {
  const isLeader = useIsLeader();
  const isAdmin = useIsAdmin();

  const { data: members, isLoading: membersLoading } = useQuery({
    queryKey: queryKeys.members(),
    queryFn: () => api.get<Member[]>("/members"),
  });
  const { data: items, isLoading: itemsLoading } = useQuery({
    queryKey: queryKeys.workItems(""),
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

  // 状态分布（donut 图数据，只保留有数量的状态）
  const statusCount = new Map<WorkItemStatus, number>();
  for (const item of all) {
    statusCount.set(item.status, (statusCount.get(item.status) ?? 0) + 1);
  }
  const statusData = STATUS_ORDER.filter((s) => (statusCount.get(s) ?? 0) > 0).map(
    (s) => ({
      name: STATUS_META[s].label,
      value: statusCount.get(s) ?? 0,
      color: STATUS_CHART_COLORS[s],
    }),
  );

  // 成员负载（横向条形图数据，按活跃任务数降序）
  const workloadMembers = (members ?? [])
    .filter((m) => m.is_active)
    .sort((a, b) => b.active_work_items - a.active_work_items);
  const workloadData = workloadMembers.map((m) => ({
    name: m.display_name,
    count: m.active_work_items,
  }));

  // 即将到期：未完成且 7 天内截止
  const upcoming = all
    .filter(
      (i) =>
        i.due_at &&
        ACTIVE_STATUSES.includes(i.status) &&
        daysUntil(i.due_at) <= 7,
    )
    .sort((a, b) => (a.due_at! < b.due_at! ? -1 : 1));

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold">团队概览</h1>
        <p className="text-sm text-muted-foreground">
          全团队的任务状态、成员负载与近期动态
        </p>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        {/* 任务状态分布（donut，中心为总数） */}
        <Card>
          <CardHeader>
            <CardTitle>任务状态分布</CardTitle>
            <CardDescription>全部任务按状态的数量占比</CardDescription>
          </CardHeader>
          <CardContent>
            {statusData.length === 0 ? (
              <p className="text-sm text-muted-foreground">暂无任务</p>
            ) : (
              <div className="relative h-64">
                <ResponsiveContainer width="100%" height="100%">
                  <PieChart>
                    <Pie
                      data={statusData}
                      dataKey="value"
                      nameKey="name"
                      innerRadius={56}
                      outerRadius={84}
                      paddingAngle={2}
                    >
                      {statusData.map((entry) => (
                        <Cell key={entry.name} fill={entry.color} />
                      ))}
                    </Pie>
                    <Tooltip />
                    <Legend verticalAlign="bottom" iconSize={10} />
                  </PieChart>
                </ResponsiveContainer>
                <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center pb-8">
                  <span className="text-2xl font-semibold">{all.length}</span>
                  <span className="text-xs text-muted-foreground">总任务</span>
                </div>
              </div>
            )}
          </CardContent>
        </Card>

        {/* 成员任务负载（横向条形图，降序） */}
        <Card>
          <CardHeader>
            <CardTitle>成员任务负载</CardTitle>
            <CardDescription>每名成员当前的活跃任务数</CardDescription>
          </CardHeader>
          <CardContent>
            {workloadData.length === 0 ? (
              <p className="text-sm text-muted-foreground">暂无成员</p>
            ) : (
              <div className="h-64">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={workloadData} layout="vertical">
                    <XAxis type="number" allowDecimals={false} hide />
                    <YAxis
                      type="category"
                      dataKey="name"
                      width={72}
                      tickLine={false}
                      axisLine={false}
                      fontSize={12}
                    />
                    <Tooltip />
                    <Bar
                      dataKey="count"
                      name="活跃任务"
                      fill="#3b82f6"
                      radius={[0, 4, 4, 0]}
                      maxBarSize={20}
                    />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        {/* 全员工作量（表格：角色与每周可投入等图表无法承载的信息） */}
        <Card>
          <CardHeader>
            <CardTitle>全员工作量</CardTitle>
            <CardDescription>每名成员当前的活跃任务数</CardDescription>
          </CardHeader>
          <CardContent className="max-h-72 overflow-y-auto">
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
                {workloadMembers.map((m) => (
                  <TableRow key={m.id}>
                    <TableCell className="font-medium">
                      {m.display_name}
                    </TableCell>
                    <TableCell>
                      <Badge variant={roleBadgeVariant(m.role)}>
                        {roleLabel(m.role)}
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
              未完成且 7 天内截止的任务（含已逾期）
            </CardDescription>
          </CardHeader>
          <CardContent className="max-h-72 overflow-y-auto">
            {upcoming.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                暂无即将到期的任务
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

      {/* 项目时间线：审计事件流（13.1 节，负责人与管理员只读可见） */}
      {(isLeader || isAdmin) && <TimelineSection />}
    </div>
  );
}
