import { useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
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
import { cn } from "@/lib/utils";
import { api } from "../../services/api";
import type {
  AgentConfig,
  AgentSuggestion,
  ApprovalItem,
  Member,
  WorkItemStatus,
  WorkItemSummary,
} from "../../types";
import {
  STATUS_META,
  formatDate,
  formatDateTime,
} from "../work-items/constants";
import { useAuthStore, useIsAdmin, useIsLeader } from "../../app/store";
import { roleBadgeVariant, roleLabel } from "../../lib/roles";
import {
  SUGGESTION_TYPE_META,
  suggestionTypeLabel,
} from "../agent-assistant/constants";
import { RequirementPipelineWizard } from "../agent-assistant/RequirementPipelineWizard";
import { TodoSection } from "./TodoSection";
import { TimelineSection } from "./TimelineSection";

const STATUS_ORDER: WorkItemStatus[] = [
  "DRAFT",
  "READY",
  "IN_PROGRESS",
  "BLOCKED",
  "IN_REVIEW",
  "COMPLETED",
  "CANCELLED",
];

/** 未完成任务状态（"我的待办"与到期统计共用）。 */
const ACTIVE_STATUSES: WorkItemStatus[] = [
  "READY",
  "IN_PROGRESS",
  "BLOCKED",
  "IN_REVIEW",
];

/** 距今天数（按截止时间计）。 */
function daysUntil(dueAt: string): number {
  const ms = new Date(dueAt).getTime() - Date.now();
  return Math.ceil(ms / (24 * 60 * 60 * 1000));
}

/** 顶部统计卡：整卡可点击跳转到对应页面。 */
function StatCard({
  label,
  value,
  to,
  alert = false,
}: {
  label: string;
  value: number;
  to: string;
  alert?: boolean;
}) {
  return (
    <Link to={to}>
      <Card className="h-full transition-colors hover:bg-accent/50">
        <CardHeader className="p-4 pb-2">
          <CardDescription>{label}</CardDescription>
          <CardTitle
            className={cn("text-2xl", alert && value > 0 && "text-destructive")}
          >
            {value}
          </CardTitle>
        </CardHeader>
      </Card>
    </Link>
  );
}

/**
 * 个人工作台：顶部统计卡 + 我的待办 + AI 动态，下方保留团队概览
 * （状态分布、全员工作量、即将到期、项目时间线，13.2 节）。
 * 全部由既有列表接口前端聚合，无后端改动。
 */
export default function DashboardPage() {
  const isLeader = useIsLeader();
  const isAdmin = useIsAdmin();
  const selfMember = useAuthStore((s) => s.member);
  // AI 需求拆解向导：resume 为 null 是新建模式，传入建议是恢复模式（"去确认"入口）
  const [wizard, setWizard] = useState<{ resume: AgentSuggestion | null } | null>(
    null,
  );

  const { data: members, isLoading: membersLoading } = useQuery({
    queryKey: ["members"],
    queryFn: () => api.get<Member[]>("/members"),
  });
  const { data: items, isLoading: itemsLoading } = useQuery({
    queryKey: ["work-items", ""],
    queryFn: () => api.get<WorkItemSummary[]>("/work-items"),
  });
  // 待我审批数（仅负责人有审批权限）
  const { data: approvals } = useQuery({
    queryKey: ["approvals"],
    queryFn: () => api.get<ApprovalItem[]>("/approvals"),
    enabled: isLeader,
  });
  // AI 动态 + 待反馈建议数（列表全员可读，反馈操作仅负责人）
  const { data: suggestions } = useQuery({
    queryKey: ["agent-suggestions", "dashboard"],
    queryFn: () => api.get<AgentSuggestion[]>("/agent-suggestions?limit=50"),
  });
  // 向导需要的外部模型提示配置
  const { data: config } = useQuery({
    queryKey: ["config"],
    queryFn: () => api.get<AgentConfig>("/config"),
    enabled: isLeader,
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
  const myItems = all.filter((i) => i.assignee.id === selfMember?.id);
  const myActive = myItems.filter((i) => ACTIVE_STATUSES.includes(i.status));

  // 我的待办：未完成，按 DDL 升序（无 DDL 排最后）
  const myTodo = [...myActive].sort((a, b) => {
    if (!a.due_at && !b.due_at) return 0;
    if (!a.due_at) return 1;
    if (!b.due_at) return -1;
    return a.due_at < b.due_at ? -1 : 1;
  });

  const inProgressCount = myItems.filter(
    (i) => i.status === "IN_PROGRESS",
  ).length;
  const overdueCount = myActive.filter(
    (i) => i.due_at && daysUntil(i.due_at) < 0,
  ).length;
  const dueTodayCount = myActive.filter(
    (i) => i.due_at && daysUntil(i.due_at) === 0,
  ).length;
  const pendingSuggestions = (suggestions ?? []).filter(
    (s) => s.review_status === "pending",
  );
  const suggestionFeed = (suggestions ?? []).slice(0, 5);

  // 状态分布
  const statusCount = new Map<WorkItemStatus, number>();
  for (const item of all) {
    statusCount.set(item.status, (statusCount.get(item.status) ?? 0) + 1);
  }

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
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">工作台</h1>
        <p className="text-sm text-muted-foreground">
          我的任务、审批与 AI 动态一览；团队整体情况见下方团队概览
        </p>
      </div>

      {/* 统计卡：点击跳转到对应页面 */}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatCard label="我的进行中任务" value={inProgressCount} to="/work-items" />
        <StatCard
          label="今日到期 / 已逾期"
          value={dueTodayCount + overdueCount}
          to="/work-items"
          alert={overdueCount > 0}
        />
        {isLeader && (
          <StatCard
            label="待我审批"
            value={approvals?.length ?? 0}
            to="/approvals"
          />
        )}
        <StatCard
          label="待反馈 AI 建议"
          value={pendingSuggestions.length}
          to="/agent-assistant"
        />
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <div className="space-y-4 lg:col-span-2">
          {/* 待处理中心：需要当前用户动作的事项聚合（13.2 节） */}
          <TodoSection />

          {/* 我的待办：我作为主执行人的未完成任务，按 DDL 升序 */}
          <Card>
            <CardHeader>
              <CardTitle>我的待办</CardTitle>
              <CardDescription>
                我负责的任务，按截止时间排序，逾期标红
              </CardDescription>
            </CardHeader>
            <CardContent>
              {myTodo.length === 0 ? (
                <p className="text-sm text-muted-foreground">
                  暂无进行中的任务
                </p>
              ) : (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>标题</TableHead>
                      <TableHead>状态</TableHead>
                      <TableHead>截止时间</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {myTodo.map((i) => {
                      const overdue = i.due_at !== null && daysUntil(i.due_at) < 0;
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
                          <TableCell>
                            <span
                              className={
                                overdue ? "font-medium text-destructive" : ""
                              }
                            >
                              {i.due_at ? formatDate(i.due_at) : "—"}
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

        {/* AI 动态：最近的建议；pipeline 待反馈的可直接打开向导确认 */}
        <Card className="self-start">
          <CardHeader>
            <CardTitle>AI 动态</CardTitle>
            <CardDescription>最近的 AI 建议与待确认事项</CardDescription>
          </CardHeader>
          <CardContent>
            {suggestionFeed.length === 0 ? (
              <p className="text-sm text-muted-foreground">暂无 AI 建议</p>
            ) : (
              <ul className="space-y-2">
                {suggestionFeed.map((s) => (
                  <li key={s.id} className="space-y-1 rounded-md border px-3 py-2">
                    <div className="flex items-center justify-between gap-2">
                      <Badge
                        className={
                          SUGGESTION_TYPE_META[s.suggestion_type]?.className ??
                          ""
                        }
                      >
                        {suggestionTypeLabel(s.suggestion_type)}
                      </Badge>
                      <span className="text-xs text-muted-foreground">
                        {formatDateTime(s.created_at)}
                      </span>
                    </div>
                    <p className="line-clamp-2 text-sm">
                      {s.content.summary ?? "（无摘要）"}
                    </p>
                    <div>
                      {isLeader &&
                      s.suggestion_type === "pipeline" &&
                      s.review_status === "pending" ? (
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={() => setWizard({ resume: s })}
                        >
                          去确认
                        </Button>
                      ) : (
                        <Link
                          to="/agent-assistant"
                          className="text-xs text-primary hover:underline"
                        >
                          查看详情
                        </Link>
                      )}
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
      </div>

      {/* 团队概览：原团队看板内容，保持不变 */}
      <div className="space-y-4">
        <div>
          <h2 className="text-lg font-semibold">团队概览</h2>
          <p className="text-sm text-muted-foreground">
            全团队的任务状态、工作量与近期动态
          </p>
        </div>

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
                    // 管理员不参与工作协作，不进入工作量统计
                    .filter((m) => m.is_active && m.role !== "admin")
                    .map((m) => (
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
            <CardContent>
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

      <RequirementPipelineWizard
        open={wizard !== null}
        onOpenChange={(next) => {
          if (!next) setWizard(null);
        }}
        members={members ?? []}
        llmIsExternal={config?.llm_is_external ?? false}
        resumeSuggestion={wizard?.resume ?? null}
      />
    </div>
  );
}
