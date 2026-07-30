import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, ChevronRight, RotateCcw, Sparkles } from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { api, errorMessage, newIdempotencyKey } from "../../services/api";
import { useIsLeader } from "../../app/store";
import type {
  AgentConfig,
  AgentRun,
  AgentSuggestion,
  Member,
} from "../../types";
import { formatDateTime } from "../work-items/constants";
import {
  agentTypeLabel,
  REVIEW_STATUS_META,
  RUN_STATUS_META,
  SUGGESTION_TYPE_META,
  suggestionTypeLabel,
} from "./constants";
import { SuggestionContent } from "./SuggestionContent";
import { RequirementPipelineWizard } from "./RequirementPipelineWizard";

/**
 * Agent 建议中心（13.1 节，T5.7）：建议列表 + 过滤 + 采纳/忽略反馈 +
 * 失败运行人工重新触发。全员可读，反馈操作仅负责人（后端同步强校验）。
 */
export default function AgentAssistantPage() {
  const isLeader = useIsLeader();
  const [typeFilter, setTypeFilter] = useState("all");
  const [statusFilter, setStatusFilter] = useState("all");
  // 拆解向导：resume 为 null 表示新建模式；传入既有 pipeline 建议则为恢复模式
  const [wizard, setWizard] = useState<{ resume: AgentSuggestion | null } | null>(
    null,
  );

  // 16 节：使用云端模型时提示"数据将发送至外部服务"
  const { data: config } = useQuery({
    queryKey: ["config"],
    queryFn: () => api.get<AgentConfig>("/config"),
  });

  const params = new URLSearchParams();
  if (typeFilter !== "all") params.set("suggestion_type", typeFilter);
  if (statusFilter !== "all") params.set("review_status", statusFilter);
  const queryString = params.toString();

  const { data: suggestions, isLoading } = useQuery({
    queryKey: ["agent-suggestions", queryString],
    queryFn: () =>
      api.get<AgentSuggestion[]>(
        `/agent-suggestions${queryString ? `?${queryString}` : ""}`,
      ),
  });

  const { data: members } = useQuery({
    queryKey: ["members"],
    queryFn: () => api.get<Member[]>("/members"),
    enabled: isLeader,
  });

  return (
    <div className="space-y-4">
      {config?.llm_is_external && (
        <p className="rounded-md border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-800">
          当前使用云端模型服务（{config.llm_provider}
          ），Agent 分析所需的数据将发送至外部服务，请勿在输入中包含敏感信息（16
          节）。
        </p>
      )}

      <Card>
        <CardHeader className="flex-row items-center justify-between space-y-0">
          <div>
            <CardTitle>Agent 建议中心</CardTitle>
            <CardDescription>
              Agent 只产出建议，不产生任何业务写入；采纳后的动作由人工确认后走正式流程
            </CardDescription>
          </div>
          {isLeader && (
            <Button variant="outline" onClick={() => setWizard({ resume: null })}>
              <Sparkles className="size-4" />
              需求拆解向导
            </Button>
          )}
        </CardHeader>
        <CardContent className="space-y-4">
          {/* 过滤区 */}
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <div className="space-y-1">
              <Label>建议类型</Label>
              <Select value={typeFilter} onValueChange={setTypeFilter}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">全部</SelectItem>
                  {Object.entries(SUGGESTION_TYPE_META).map(([value, meta]) => (
                    <SelectItem key={value} value={value}>
                      {meta.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1">
              <Label>反馈状态</Label>
              <Select value={statusFilter} onValueChange={setStatusFilter}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">全部</SelectItem>
                  {Object.entries(REVIEW_STATUS_META).map(([value, meta]) => (
                    <SelectItem key={value} value={value}>
                      {meta.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>

          {isLoading ? (
            <p className="text-sm text-muted-foreground">加载中…</p>
          ) : !suggestions || suggestions.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              暂无符合条件的建议。可在工作项详情或本页触发 Agent 分析。
            </p>
          ) : (
            <div className="space-y-3">
              {suggestions.map((s) => (
                <SuggestionCard
                  key={s.id}
                  suggestion={s}
                  onAdoptPipeline={(target) => setWizard({ resume: target })}
                />
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      <AgentRunsCard />

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

/** 单条建议卡片：类型/状态徽标 + 摘要，展开查看完整内容与反馈操作。 */
function SuggestionCard({
  suggestion,
  onAdoptPipeline,
}: {
  suggestion: AgentSuggestion;
  /** pipeline 建议的"采纳"：打开拆解向导确认后批量创建工作项。 */
  onAdoptPipeline: (suggestion: AgentSuggestion) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const isLeader = useIsLeader();
  const queryClient = useQueryClient();

  const feedback = useMutation({
    mutationFn: (action: "accepted" | "ignored") =>
      api.post<AgentSuggestion>(
        `/agent-suggestions/${suggestion.id}/feedback`,
        { action },
        newIdempotencyKey(),
      ),
    onSuccess: (_data, action) => {
      toast.success(action === "accepted" ? "已采纳该建议" : "已忽略该建议");
      queryClient.invalidateQueries({ queryKey: ["agent-suggestions"] });
    },
    onError: (error) => toast.error(errorMessage(error, "反馈提交失败")),
  });

  const typeMeta = SUGGESTION_TYPE_META[suggestion.suggestion_type];
  const statusMeta = REVIEW_STATUS_META[suggestion.review_status];

  return (
    <Card>
      <CardHeader className="space-y-2">
        <div className="flex items-center justify-between gap-2">
          <div className="flex flex-wrap items-center gap-2">
            <Badge className={typeMeta?.className ?? ""}>
              {suggestionTypeLabel(suggestion.suggestion_type)}
            </Badge>
            <Badge className={statusMeta?.className ?? ""}>
              {statusMeta?.label ?? suggestion.review_status}
            </Badge>
            {suggestion.confidence !== null && (
              <span className="text-sm text-muted-foreground">
                置信度 {Math.round(suggestion.confidence * 100)}%
              </span>
            )}
            {suggestion.work_item_id ? (
              <Link
                to={`/work-items/${suggestion.work_item_id}`}
                className="text-sm text-primary hover:underline"
              >
                关联工作项
              </Link>
            ) : (
              <span className="text-sm text-muted-foreground">项目级建议</span>
            )}
          </div>
          <span className="shrink-0 text-sm text-muted-foreground">
            {formatDateTime(suggestion.created_at)}
          </span>
        </div>
        <CardDescription>
          {suggestion.content.summary ?? "（无摘要）"}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <Button
          size="sm"
          variant="ghost"
          onClick={() => setExpanded((v) => !v)}
        >
          {expanded ? (
            <ChevronDown className="size-4" />
          ) : (
            <ChevronRight className="size-4" />
          )}
          {expanded ? "收起详情" : "展开详情"}
        </Button>

        {expanded && (
          <div className="space-y-3 text-sm">
            {suggestion.content.rationale && (
              <div>
                <h4 className="mb-1 font-medium text-muted-foreground">理由</h4>
                <p className="whitespace-pre-wrap">
                  {suggestion.content.rationale}
                </p>
              </div>
            )}
            <SuggestionContent
              suggestionType={suggestion.suggestion_type}
              content={suggestion.content}
            />
            {suggestion.risks && (
              <div>
                <h4 className="mb-1 font-medium text-muted-foreground">
                  风险和限制
                </h4>
                <p className="whitespace-pre-wrap rounded-md bg-amber-50 px-3 py-2 text-amber-800">
                  {suggestion.risks}
                </p>
              </div>
            )}
            {suggestion.fact_refs &&
              Object.keys(suggestion.fact_refs).length > 0 && (
                <div>
                  <h4 className="mb-1 font-medium text-muted-foreground">
                    事实引用
                  </h4>
                  <ul className="space-y-0.5 text-xs text-muted-foreground">
                    {Object.entries(suggestion.fact_refs).map(([key, ids]) => (
                      <li key={key}>
                        {key}：{(ids ?? []).join("、") || "（无）"}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            <p className="text-xs text-muted-foreground">
              模型：{suggestion.model ?? "未知"}；提示词版本：
              {suggestion.prompt_version ?? "未知"}；运行 ID：{suggestion.run_id}
              {suggestion.reviewed_at &&
                `；反馈时间：${formatDateTime(suggestion.reviewed_at)}`}
            </p>
            {isLeader && suggestion.review_status === "pending" && (
              <div className="flex gap-2">
                {suggestion.suggestion_type === "pipeline" ? (
                  <Button size="sm" onClick={() => onAdoptPipeline(suggestion)}>
                    采纳并创建工作项
                  </Button>
                ) : (
                  <Button
                    size="sm"
                    disabled={feedback.isPending}
                    onClick={() => feedback.mutate("accepted")}
                  >
                    采纳
                  </Button>
                )}
                <Button
                  size="sm"
                  variant="outline"
                  disabled={feedback.isPending}
                  onClick={() => feedback.mutate("ignored")}
                >
                  忽略
                </Button>
              </div>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

/** 运行记录：展示状态/耗时/错误，failed 运行可人工重新触发（T5.6 入口）。 */
function AgentRunsCard() {
  const queryClient = useQueryClient();

  const { data: runs } = useQuery({
    queryKey: ["agent-runs"],
    queryFn: () => api.get<AgentRun[]>("/agent-runs?limit=20"),
  });

  const retry = useMutation({
    mutationFn: (runId: string) =>
      api.post<AgentRun>(`/agent-runs/${runId}/retry`, {}, newIdempotencyKey()),
    onSuccess: () => {
      toast.success("已重新触发，请稍候查看结果");
      queryClient.invalidateQueries({ queryKey: ["agent-runs"] });
      queryClient.invalidateQueries({ queryKey: ["agent-suggestions"] });
    },
    onError: (error) => toast.error(errorMessage(error, "重新触发失败")),
  });

  if (!runs || runs.length === 0) {
    return null;
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>运行记录</CardTitle>
        <CardDescription>
          最近的 Agent 运行；失败的运行可人工重新触发
        </CardDescription>
      </CardHeader>
      <CardContent>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Agent</TableHead>
              <TableHead>状态</TableHead>
              <TableHead>触发来源</TableHead>
              <TableHead>耗时</TableHead>
              <TableHead>时间</TableHead>
              <TableHead className="text-right">操作</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {runs.map((run) => (
              <TableRow key={run.id}>
                <TableCell>{agentTypeLabel(run.agent_type)}</TableCell>
                <TableCell>
                  <Badge
                    className={RUN_STATUS_META[run.status]?.className ?? ""}
                    title={run.error ?? undefined}
                  >
                    {RUN_STATUS_META[run.status]?.label ?? run.status}
                  </Badge>
                  {run.status === "failed" && run.error && (
                    <p className="mt-1 max-w-xs truncate text-xs text-muted-foreground">
                      {run.error}
                    </p>
                  )}
                </TableCell>
                <TableCell>
                  {run.trigger_source === "manual"
                    ? "人工"
                    : run.trigger_source === "scheduler"
                      ? "周期"
                      : "事件"}
                </TableCell>
                <TableCell>
                  {run.duration_ms !== null
                    ? `${(run.duration_ms / 1000).toFixed(1)}s`
                    : "—"}
                </TableCell>
                <TableCell>{formatDateTime(run.created_at)}</TableCell>
                <TableCell className="text-right">
                  {run.status === "failed" && (
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={retry.isPending}
                      onClick={() => retry.mutate(run.id)}
                    >
                      <RotateCcw className="size-4" />
                      重新触发
                    </Button>
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}
