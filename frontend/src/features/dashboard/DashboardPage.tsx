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
import { api } from "../../services/api";
import type {
  AgentConfig,
  AgentSuggestion,
  ApprovalItem,
  Member,
  WorkItemSummary,
} from "../../types";
import {
  STATUS_META,
  formatDate,
  formatDateTime,
} from "../work-items/constants";
import { useAuthStore, useIsLeader } from "../../app/store";
import {
  SUGGESTION_TYPE_META,
  suggestionTypeLabel,
} from "../agent-assistant/constants";
import { RequirementPipelineWizard } from "../agent-assistant/RequirementPipelineWizard";
import { TodoSection } from "./TodoSection";
import { ACTIVE_STATUSES, StatCard, daysUntil } from "./shared";

/**
 * 个人工作台：顶部统计卡 + 我的待办 + AI 动态（一屏内展示，列表内部滚动）。
 * 团队视角内容已拆至 /team-overview。
 */
export default function DashboardPage() {
  const isLeader = useIsLeader();
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

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold">工作台</h1>
        <p className="text-sm text-muted-foreground">
          我的任务、审批与 AI 动态一览；团队整体情况见「团队概览」
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

          {/* 我的待办：我作为主执行人的未完成任务，按 DDL 升序；超长内部滚动 */}
          <Card>
            <CardHeader>
              <CardTitle>我的待办</CardTitle>
              <CardDescription>
                我负责的任务，按截止时间排序，逾期标红
              </CardDescription>
            </CardHeader>
            <CardContent className="max-h-80 overflow-y-auto">
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

        {/* AI 动态：最近的建议；pipeline 待反馈的可直接打开向导确认；超长内部滚动 */}
        <Card className="self-start">
          <CardHeader>
            <CardTitle>AI 动态</CardTitle>
            <CardDescription>最近的 AI 建议与待确认事项</CardDescription>
          </CardHeader>
          <CardContent className="max-h-96 overflow-y-auto">
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
