import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  api,
  ApiError,
  errorMessage,
  newIdempotencyKey,
} from "../../services/api";
import { queryKeys } from "../../lib/queryKeys";
import type {
  AgentRun,
  AgentSuggestion,
  Deliverable,
  DevDoc,
  WorkItem,
} from "../../types";
import { SuggestionContent } from "../agent-assistant/SuggestionContent";
import { formatDateTime } from "./constants";

type ReviewType = "dev_doc_review" | "deliverable_review";

const RUN_STATUS = {
  pending: "等待初审",
  running: "初审运行中",
  succeeded: "初审运行完成",
  failed: "初审运行失败",
};

interface Props {
  workItemId: string;
  workItem?: WorkItem;
  agentType: ReviewType;
  devDoc?: DevDoc | null;
  deliverable?: Deliverable | null;
}

export function ReviewOpinion({
  suggestion,
  workItem,
  devDoc,
  deliverable,
}: {
  suggestion: AgentSuggestion;
  workItem?: WorkItem;
  devDoc?: DevDoc | null;
  deliverable?: Deliverable | null;
}) {
  const context = suggestion.content.review_context;
  const isDocReview = suggestion.suggestion_type === "dev_doc_review";
  const docMatches = context?.dev_doc
    ? devDoc !== undefined &&
      context.dev_doc.id === devDoc?.id &&
      context.dev_doc.doc_version === devDoc.doc_version &&
      context.dev_doc.content === devDoc.content
    : devDoc === null;
  const materialKnown =
    !!context &&
    (!workItem || !!context.work_item) &&
    devDoc !== undefined &&
    (isDocReview
      ? !!context.dev_doc
      : !!context.deliverable && deliverable !== undefined);
  const current =
    materialKnown &&
    (!workItem || (
      context.work_item?.id === workItem.id &&
      context.work_item.title === workItem.title &&
      context.work_item.description === workItem.description &&
      context.work_item.acceptance_criteria === workItem.acceptance_criteria
    )) &&
    docMatches &&
    (isDocReview ||
      (context.deliverable?.id === deliverable?.id &&
        context.deliverable?.version === deliverable?.version));

  return (
    <article className="space-y-2 rounded-md border p-3">
      <div className="flex flex-wrap items-center gap-2">
        <h4 className="font-medium">
          {current
            ? "当前材料初审意见"
            : materialKnown
              ? "历史初审意见"
              : "初审意见（版本待确认）"}
        </h4>
        <span className="text-xs text-muted-foreground">
          {formatDateTime(suggestion.created_at)}
        </span>
      </div>
      {!context && (
        <p className="text-muted-foreground">
          该意见未记录材料快照，无法确认对应版本。
        </p>
      )}
      {context && !materialKnown && (
        <p className="text-muted-foreground">
          当前材料信息不足，无法确认意见对应版本。
        </p>
      )}
      {materialKnown && !current && (
        <p className="text-amber-700">
          任务要求或材料已更新，此意见针对历史材料，仅供参考。
        </p>
      )}
      {suggestion.content.summary && (
        <p className="whitespace-pre-wrap">{suggestion.content.summary}</p>
      )}
      {suggestion.content.rationale && (
        <p className="whitespace-pre-wrap text-muted-foreground">
          {suggestion.content.rationale}
        </p>
      )}
      <SuggestionContent
        suggestionType={suggestion.suggestion_type}
        content={suggestion.content}
      />
      {context && (
        <details className="rounded-md bg-muted/50 p-2">
          <summary className="cursor-pointer">审阅材料与约定快照</summary>
          <div className="mt-2 space-y-2">
            {context.work_item && (
              <div>
                <p>任务：{context.work_item.title} · 记录版本 v{context.work_item.version}</p>
                <p className="whitespace-pre-wrap">{context.work_item.description}</p>
                <p className="whitespace-pre-wrap">验收标准：{context.work_item.acceptance_criteria || "未填写"}</p>
              </div>
            )}
            {context.dev_doc ? (
              <div>
                <p>
                  开发文档：第 {context.dev_doc.doc_version} 次提交 · 记录版本 v
                  {context.dev_doc.version}
                </p>
                <pre className="max-h-60 overflow-auto whitespace-pre-wrap">
                  {context.dev_doc.content}
                </pre>
              </div>
            ) : (
              <p>审阅时未提供开发文档</p>
            )}
            {context.deliverable && (
              <p>交付物：第 {context.deliverable.version} 版</p>
            )}
            <h5 className="font-medium">项目约定快照</h5>
            {!context.core_memory_loaded ? (
              <p>项目约定加载失败，本次初审未获得完整约定。</p>
            ) : context.core_memory.length === 0 ? (
              <p>审阅时没有有效项目约定</p>
            ) : (
              <ul className="list-disc space-y-1 pl-5">
                {context.core_memory.map((entry) => (
                  <li key={entry.id} className="whitespace-pre-wrap">
                    {entry.content}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </details>
      )}
    </article>
  );
}

export function TaskReviewPanel({
  workItemId,
  workItem,
  agentType,
  devDoc,
  deliverable,
}: Props) {
  const queryClient = useQueryClient();
  const suggestionType =
    agentType === "dev_doc_review" ? "dev_doc_review" : "review";
  const runsKey = queryKeys.agentRuns("task-review", workItemId, agentType, "latest");
  const suggestionsKey = queryKeys.agentSuggestions(
    "task-review",
    workItemId,
    suggestionType,
    "latest",
  );
  const runs = useQuery({
    queryKey: runsKey,
    queryFn: () =>
      api.get<AgentRun[]>(
        `/agent-runs?${new URLSearchParams({ work_item_id: workItemId, agent_type: agentType, limit: "1" })}`,
      ),
    refetchInterval: 5000,
    retry: false,
  });
  const suggestions = useQuery({
    queryKey: suggestionsKey,
    queryFn: () =>
      api.get<AgentSuggestion[]>(
        `/agent-suggestions?${new URLSearchParams({ work_item_id: workItemId, suggestion_type: suggestionType, limit: "1" })}`,
      ),
    refetchInterval: 5000,
    retry: false,
  });
  const latestRun = runs.data
    ?.filter(
      (run) => run.work_item_id === workItemId && run.agent_type === agentType,
    )
    .sort((a, b) => b.created_at.localeCompare(a.created_at))[0];
  const latestSuggestion = suggestions.data
    ?.filter(
      (suggestion) =>
        suggestion.work_item_id === workItemId &&
        suggestion.suggestion_type === suggestionType,
    )
    .sort((a, b) => b.created_at.localeCompare(a.created_at))[0];
  const retry = useMutation({
    mutationFn: (run: AgentRun) =>
      api.post<AgentRun>(
        `/agent-runs/${run.id}/retry`,
        {},
        newIdempotencyKey(),
      ),
    onSettled: () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: runsKey }),
        queryClient.invalidateQueries({ queryKey: suggestionsKey }),
      ]),
  });

  return (
    <section
      aria-label={
        agentType === "dev_doc_review" ? "开发文档 AI 初审" : "交付物 AI 初审"
      }
      className="space-y-3 rounded-md border p-3 text-sm"
    >
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="font-medium">
          {agentType === "dev_doc_review"
            ? "开发文档 AI 初审"
            : "交付物 AI 初审"}
        </h3>
        {latestRun && (
          <Badge variant="outline">{RUN_STATUS[latestRun.status]}</Badge>
        )}
      </div>
      <p className="text-xs text-muted-foreground">
        初审仅供参考，正式确认由负责人决定；AI 状态不影响提交与审核操作。
      </p>
      {(runs.isError || suggestions.isError) && (
        <p role="status">初审信息暂时无法加载，正式操作仍可继续。</p>
      )}
      {runs.isLoading && <p role="status">正在加载初审状态…</p>}
      {!runs.isLoading && !runs.isError && !latestRun && (
        <p>暂无初审运行记录，提交后自动触发。</p>
      )}
      {latestRun && (
        <p className="text-xs text-muted-foreground">
          最近运行：{formatDateTime(latestRun.created_at)} · 重试{" "}
          {latestRun.retry_count} 次
        </p>
      )}
      {latestRun?.status === "failed" && (
        <div className="space-y-2">
          <p className="whitespace-pre-wrap text-destructive">
            {latestRun.error || "初审未能完成"}
          </p>
          <Button
            size="sm"
            variant="outline"
            disabled={retry.isPending}
            onClick={() => retry.mutate(latestRun)}
          >
            {retry.isPending ? "正在重试…" : "重试初审"}
          </Button>
        </div>
      )}
      {retry.isError && (
        <p role="alert" className="text-destructive">
          {errorMessage(retry.error, "初审重试失败")}
        </p>
      )}
      {latestRun &&
        latestSuggestion &&
        latestRun.id !== latestSuggestion.run_id && (
          <p className="text-muted-foreground">
            下方为此前运行产出的最近意见，并非最近运行的结果。
          </p>
        )}
      {latestSuggestion ? (
        <ReviewOpinion
          suggestion={latestSuggestion}
          workItem={workItem}
          devDoc={devDoc}
          deliverable={deliverable}
        />
      ) : (
        !suggestions.isLoading && !suggestions.isError && <p>暂无初审意见。</p>
      )}
    </section>
  );
}

export function DeliverableReviewPanel({ workItemId, workItem }: { workItemId: string; workItem?: WorkItem }) {
  const deliverables = useQuery({
    queryKey: queryKeys.deliverables(workItemId),
    queryFn: () =>
      api.get<Deliverable[]>(`/work-items/${workItemId}/deliverables`),
    retry: false,
  });
  const doc = useQuery({
    queryKey: queryKeys.devDoc(workItemId),
    queryFn: () => api.get<DevDoc>(`/work-items/${workItemId}/dev-doc`),
    retry: false,
  });
  if (deliverables.isError || !deliverables.data) return null;
  const latest = deliverables.data[0] ?? null;
  return (
    <TaskReviewPanel
      workItemId={workItemId}
      workItem={workItem}
      agentType="deliverable_review"
      deliverable={latest}
      devDoc={
        doc.error instanceof ApiError && doc.error.status === 404
          ? null
          : doc.isError || !doc.data
            ? undefined
            : doc.data.status === "CONFIRMED"
              ? doc.data
              : null
      }
    />
  );
}
