import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
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
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";
import {
  api,
  ApiError,
  errorMessage,
  newIdempotencyKey,
  VERSION_CONFLICT_MESSAGE,
} from "../../services/api";
import { useAuthStore, useIsLeader } from "../../app/store";
import type { AgentSuggestion, DevDoc, WorkItem } from "../../types";
import { DEV_DOC_STATUS_META, formatDateTime } from "./constants";
import {
  DEV_DOC_VERDICT_META,
  SuggestionContent,
} from "../agent-assistant/SuggestionContent";

interface Props {
  workItem: WorkItem;
}

/** AI 初审意见面板：按 latest_review_suggestion_id 从建议列表中定位；初审未产出（LLM 降级）时不渲染、不阻塞。 */
export function DevDocReviewPanel({ suggestionId }: { suggestionId: string }) {
  const { data: suggestions } = useQuery({
    queryKey: ["agent-suggestions", "dev-doc-review", suggestionId],
    queryFn: () => api.get<AgentSuggestion[]>("/agent-suggestions?limit=50"),
  });
  const suggestion = suggestions?.find((s) => s.id === suggestionId);
  if (!suggestion) {
    return null;
  }
  const verdict =
    typeof suggestion.content.verdict === "string"
      ? suggestion.content.verdict
      : "";
  const verdictMeta = DEV_DOC_VERDICT_META[verdict];
  return (
    <div className="space-y-2 rounded-md border p-3 text-sm">
      <div className="flex items-center gap-2">
        <h4 className="font-medium">AI 初审意见</h4>
        {verdict && (
          <Badge className={verdictMeta?.className ?? ""}>
            {verdictMeta?.label ?? verdict}
          </Badge>
        )}
        <span className="text-xs text-muted-foreground">
          仅供参考，确认由负责人决定
        </span>
      </div>
      <SuggestionContent
        suggestionType={suggestion.suggestion_type}
        content={suggestion.content}
      />
    </div>
  );
}

/**
 * 开发文档区（2026-07-30 设计文档 §5）：先文档后开发。
 * 主执行人：撰写/编辑（textarea + 预览切换）、保存草稿、提交审核；被打回显示理由可重交。
 * 负责人：只读查看 + 确认/打回/豁免。无文档时 GET 返回 404，按"未创建"处理。
 */
export function DevDocSection({ workItem }: Props) {
  const queryClient = useQueryClient();
  const isLeader = useIsLeader();
  const selfMember = useAuthStore((s) => s.member);
  const isAssignee = selfMember?.id === workItem.assignee.id;

  const [draft, setDraft] = useState("");
  const [preview, setPreview] = useState(false);
  const [returnOpen, setReturnOpen] = useState(false);
  const [reviewNote, setReviewNote] = useState("");

  const { data: doc, isLoading, error } = useQuery({
    queryKey: ["dev-doc", workItem.id],
    queryFn: () => api.get<DevDoc>(`/work-items/${workItem.id}/dev-doc`),
    retry: false,
  });
  const notFound = error instanceof ApiError && error.status === 404;

  // 编辑器内容跟随服务端版本（自身保存后 updated_at 变化，内容一致无感知）
  useEffect(() => {
    setDraft(doc?.content ?? "");
  }, [doc?.id, doc?.updated_at]); // eslint-disable-line react-hooks/exhaustive-deps

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["dev-doc", workItem.id] });
    queryClient.invalidateQueries({ queryKey: ["work-items"] });
    queryClient.invalidateQueries({ queryKey: ["approvals"] });
    queryClient.invalidateQueries({ queryKey: ["agent-suggestions"] });
  };

  const onError = (fallback: string) => (e: unknown) => {
    if (e instanceof ApiError && e.isVersionConflict) {
      toast.error(VERSION_CONFLICT_MESSAGE);
      invalidate();
      return;
    }
    toast.error(errorMessage(e, fallback));
  };

  // 保存草稿：不存在则创建（不带 version），存在则带 version 乐观锁
  const save = useMutation({
    mutationFn: () =>
      api.put<DevDoc>(
        `/work-items/${workItem.id}/dev-doc`,
        doc ? { content: draft, version: doc.version } : { content: draft },
        newIdempotencyKey(),
      ),
    onSuccess: () => {
      toast.success("草稿已保存");
      invalidate();
    },
    onError: onError("保存草稿失败"),
  });

  const submit = useMutation({
    mutationFn: () =>
      api.post<DevDoc>(
        `/work-items/${workItem.id}/dev-doc/submit`,
        { version: doc!.version },
        newIdempotencyKey(),
      ),
    onSuccess: () => {
      toast.success("已提交审核，等待负责人确认");
      invalidate();
    },
    onError: onError("提交审核失败"),
  });

  const confirm = useMutation({
    mutationFn: () =>
      api.post<DevDoc>(
        `/work-items/${workItem.id}/dev-doc/confirm`,
        { version: doc!.version },
        newIdempotencyKey(),
      ),
    onSuccess: () => {
      toast.success("已确认开发文档");
      invalidate();
    },
    onError: onError("确认失败"),
  });

  const returnDoc = useMutation({
    mutationFn: () =>
      api.post<DevDoc>(
        `/work-items/${workItem.id}/dev-doc/return`,
        { version: doc!.version, review_note: reviewNote.trim() },
        newIdempotencyKey(),
      ),
    onSuccess: () => {
      toast.success("已打回，等待主执行人修改重交");
      setReturnOpen(false);
      setReviewNote("");
      invalidate();
    },
    onError: onError("打回失败"),
  });

  const waive = useMutation({
    mutationFn: () =>
      api.post<DevDoc>(
        `/work-items/${workItem.id}/dev-doc/waive`,
        doc ? { version: doc.version } : {},
        newIdempotencyKey(),
      ),
    onSuccess: () => {
      toast.success("已豁免该任务的文档要求");
      invalidate();
    },
    onError: onError("豁免失败"),
  });

  if (isLoading) {
    return null;
  }
  // 非 404 的读取失败（如无权限 403）：与交付区一致，静默不渲染
  if (error && !notFound) {
    return null;
  }

  const editable =
    isAssignee && (!doc || doc.status === "DRAFT" || doc.status === "RETURNED");
  const pending = save.isPending || submit.isPending;
  const leaderActing =
    confirm.isPending || returnDoc.isPending || waive.isPending;
  const statusMeta = doc ? DEV_DOC_STATUS_META[doc.status] : null;

  return (
    <Card id="dev-doc-section">
      <CardHeader>
        <div className="flex items-center gap-2">
          <CardTitle>开发文档</CardTitle>
          {statusMeta && (
            <Badge className={statusMeta.className}>{statusMeta.label}</Badge>
          )}
          {doc && (
            <span className="text-xs text-muted-foreground">
              第 {doc.doc_version} 次提交
            </span>
          )}
          {doc?.waived && (
            <Badge className="bg-muted text-muted-foreground">
              已豁免文档要求
            </Badge>
          )}
        </div>
        <CardDescription>
          开始开发前需要先提交开发文档并通过负责人确认
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {/* 打回理由（可编辑重交） */}
        {doc?.status === "RETURNED" && doc.review_note && (
          <p className="rounded-md border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-700">
            打回理由：{doc.review_note}
          </p>
        )}

        {!doc && !isAssignee && (
          <p className="text-sm text-muted-foreground">
            主执行人还没有提交开发文档
          </p>
        )}

        {editable ? (
          <div className="space-y-2">
            {!doc && (
              <p className="rounded-md bg-amber-50 px-3 py-2 text-sm text-amber-800">
                开始开发前需要先提交开发文档：写清设计思路、实现方案、接口约定与排期。
              </p>
            )}
            <div className="flex justify-end">
              <Button
                size="sm"
                variant="ghost"
                onClick={() => setPreview((v) => !v)}
              >
                {preview ? "继续编辑" : "预览"}
              </Button>
            </div>
            {preview ? (
              <pre className="min-h-40 whitespace-pre-wrap rounded-md border bg-muted/50 px-3 py-2 text-sm">
                {draft || "（空）"}
              </pre>
            ) : (
              <Textarea
                rows={12}
                placeholder="设计思路、实现方案、接口约定、排期、风险……（支持 Markdown 纯文本）"
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
              />
            )}
            <div className="flex gap-2">
              <Button
                variant="outline"
                disabled={pending || !draft.trim()}
                onClick={() => save.mutate()}
              >
                {save.isPending ? "保存中…" : "保存草稿"}
              </Button>
              {doc && (
                <Button
                  disabled={pending || !draft.trim()}
                  onClick={() => submit.mutate()}
                >
                  {submit.isPending ? "提交中…" : "提交审核"}
                </Button>
              )}
            </div>
            {doc && (
              <p className="text-xs text-muted-foreground">
                提交审核前请先保存最新修改。
              </p>
            )}
          </div>
        ) : (
          doc && (
            <pre className="max-h-96 overflow-y-auto whitespace-pre-wrap rounded-md border bg-muted/50 px-3 py-2 text-sm">
              {doc.content}
            </pre>
          )
        )}

        {doc?.latest_review_suggestion_id && (
          <DevDocReviewPanel suggestionId={doc.latest_review_suggestion_id} />
        )}

        {doc?.confirmed_at && (
          <p className="text-xs text-muted-foreground">
            由 {doc.confirmed_by?.display_name ?? "—"} 确认于{" "}
            {formatDateTime(doc.confirmed_at)}
          </p>
        )}

        {/* 负责人动作：确认/打回（待确认时）、豁免（未豁免且未确认时） */}
        {isLeader && (
          <div className="flex gap-2">
            {doc?.status === "SUBMITTED" && (
              <>
                <Button
                  size="sm"
                  disabled={leaderActing}
                  onClick={() => confirm.mutate()}
                >
                  {confirm.isPending ? "确认中…" : "确认通过"}
                </Button>
                <Button
                  size="sm"
                  variant="destructive"
                  disabled={leaderActing}
                  onClick={() => setReturnOpen(true)}
                >
                  打回
                </Button>
              </>
            )}
            {!doc?.waived && doc?.status !== "CONFIRMED" && (
              <Button
                size="sm"
                variant="outline"
                disabled={leaderActing}
                onClick={() => waive.mutate()}
              >
                {waive.isPending ? "豁免中…" : "豁免文档要求"}
              </Button>
            )}
          </div>
        )}
      </CardContent>

      {/* 打回理由对话框（review_note 必填） */}
      <Dialog open={returnOpen} onOpenChange={setReturnOpen}>
        <DialogContent className="max-w-xl">
          <DialogHeader>
            <DialogTitle>打回开发文档</DialogTitle>
            <DialogDescription>
              说明需要修改的地方，主执行人修改后可重新提交。
            </DialogDescription>
          </DialogHeader>
          <Textarea
            rows={4}
            placeholder="打回理由（必填）"
            value={reviewNote}
            onChange={(e) => setReviewNote(e.target.value)}
          />
          <DialogFooter>
            <Button
              variant="destructive"
              disabled={!reviewNote.trim() || returnDoc.isPending}
              onClick={() => returnDoc.mutate()}
            >
              {returnDoc.isPending ? "提交中…" : "确认打回"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  );
}
