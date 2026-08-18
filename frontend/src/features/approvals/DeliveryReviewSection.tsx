import { useState } from "react";
import { Link } from "react-router-dom";
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
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import {
  api,
  errorMessage,
  newIdempotencyKey,
} from "../../services/api";
import type {
  Deliverable,
  Review,
  ReviewDecision,
  WorkItemSummary,
} from "../../types";
import { formatDateTime, STATUS_META } from "../work-items/constants";
import {
  DELIVERABLE_TYPE_META,
  REVIEW_DECISION_META,
} from "../deliverables/constants";
import { DeliverableBody } from "../deliverables/DeliverableBody";
import { queryKeys } from "../../lib/queryKeys";

/**
 * 审批中心"交付审核"区（13.1 节，仅负责人）：列出 IN_REVIEW 工作项，
 * 审核 Dialog 查看当前/历史版本交付物与 reviews 历史，
 * 三选一结论提交 POST /work-items/{id}/reviews。
 */
export function DeliveryReviewSection() {
  const [target, setTarget] = useState<WorkItemSummary | null>(null);

  const { data: items, isLoading } = useQuery({
    queryKey: queryKeys.workItems("status=IN_REVIEW"),
    queryFn: () => api.get<WorkItemSummary[]>("/work-items?status=IN_REVIEW"),
  });

  if (isLoading) {
    return (
      <div className="space-y-2">
        <Skeleton className="h-24 w-full" />
        <Skeleton className="h-24 w-full" />
      </div>
    );
  }

  if (!items || items.length === 0) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>交付审核</CardTitle>
          <CardDescription>当前没有等待最终审核的工作项</CardDescription>
        </CardHeader>
      </Card>
    );
  }

  return (
    <div className="space-y-3">
      {items.map((item) => (
        <ReviewCard key={item.id} item={item} onReview={() => setTarget(item)} />
      ))}
      {target && (
        <ReviewDialog
          item={target}
          open={target !== null}
          onOpenChange={(open) => !open && setTarget(null)}
        />
      )}
    </div>
  );
}

/** 单个待审核工作项卡片：标题、主执行人、当前交付物版本摘要。 */
function ReviewCard({
  item,
  onReview,
}: {
  item: WorkItemSummary;
  onReview: () => void;
}) {
  const { data: deliverables } = useQuery({
    queryKey: queryKeys.deliverables(item.id),
    queryFn: () =>
      api.get<Deliverable[]>(`/work-items/${item.id}/deliverables`),
  });
  const latest = deliverables?.[0];

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Badge className={STATUS_META[item.status].className}>
              {STATUS_META[item.status].label}
            </Badge>
            <Link
              to={`/work-items/${item.id}`}
              className="font-medium text-primary hover:underline"
            >
              {item.title}
            </Link>
          </div>
          <span className="text-sm text-muted-foreground">
            {formatDateTime(item.updated_at)}
          </span>
        </div>
        <CardDescription>
          主执行人：{item.assignee.display_name}
        </CardDescription>
      </CardHeader>
      <CardContent className="flex items-center justify-between gap-2">
        <p className="text-sm text-muted-foreground">
          {latest
            ? `当前交付：第 ${latest.version} 版（${DELIVERABLE_TYPE_META[latest.type].label}），${latest.submitted_by.display_name} 提交于 ${formatDateTime(latest.created_at)}`
            : "加载交付物信息…"}
        </p>
        <Button size="sm" onClick={onReview}>
          审核
        </Button>
      </CardContent>
    </Card>
  );
}

/** 审核 Dialog：交付物版本切换查看、reviews 历史、三种结论提交。 */
function ReviewDialog({
  item,
  open,
  onOpenChange,
}: {
  item: WorkItemSummary;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const queryClient = useQueryClient();
  const [selectedVersion, setSelectedVersion] = useState<number | null>(null);
  const [decision, setDecision] = useState<ReviewDecision>("approve");
  const [feedback, setFeedback] = useState("");
  const [fieldError, setFieldError] = useState<string | null>(null);

  const { data: deliverables } = useQuery({
    queryKey: queryKeys.deliverables(item.id),
    queryFn: () =>
      api.get<Deliverable[]>(`/work-items/${item.id}/deliverables`),
  });

  const { data: reviews } = useQuery({
    queryKey: queryKeys.reviews(item.id),
    queryFn: () => api.get<Review[]>(`/work-items/${item.id}/reviews`),
  });

  const current =
    deliverables?.find((d) => d.version === selectedVersion) ??
    deliverables?.[0];

  const reset = () => {
    setSelectedVersion(null);
    setDecision("approve");
    setFeedback("");
    setFieldError(null);
  };

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: queryKeys.workItems() });
    queryClient.invalidateQueries({ queryKey: queryKeys.deliverables() });
    queryClient.invalidateQueries({ queryKey: queryKeys.reviews() });
    queryClient.invalidateQueries({ queryKey: queryKeys.approvals() });
    queryClient.invalidateQueries({ queryKey: queryKeys.notifications() });
  };

  const review = useMutation({
    mutationFn: () =>
      api.post<Review>(
        `/work-items/${item.id}/reviews`,
        {
          deliverable_id: current!.id,
          decision,
          feedback: feedback.trim() || null,
        },
        newIdempotencyKey(),
      ),
    onSuccess: () => {
      toast.success(`已提交审核结论：${REVIEW_DECISION_META[decision].label}`);
      invalidate();
      onOpenChange(false);
      reset();
    },
    onError: (error) => {
      toast.error(errorMessage(error, "审核提交失败"));
      invalidate();
    },
  });

  const handleSubmit = () => {
    if (!current) {
      setFieldError("交付物信息尚未加载完成");
      return;
    }
    if (decision === "request_changes" && !feedback.trim()) {
      setFieldError("要求修改时必须填写反馈");
      return;
    }
    setFieldError(null);
    review.mutate();
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(v) => {
        onOpenChange(v);
        if (!v) reset();
      }}
    >
      <DialogContent className="max-h-[90vh] max-w-2xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>交付审核：{item.title}</DialogTitle>
          <DialogDescription>
            主执行人：{item.assignee.display_name}。通过则工作项完成；
            要求修改将退回进行中；拒绝保持审核中（7.5 节）。
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          <section className="space-y-2">
            <div className="flex items-center gap-2">
              <Label>交付物版本</Label>
              <Select
                value={String(current?.version ?? "")}
                onValueChange={(v) => setSelectedVersion(Number(v))}
              >
                <SelectTrigger className="w-40">
                  <SelectValue placeholder="选择版本" />
                </SelectTrigger>
                <SelectContent>
                  {(deliverables ?? []).map((d) => (
                    <SelectItem key={d.id} value={String(d.version)}>
                      第 {d.version} 版（{DELIVERABLE_TYPE_META[d.type].label}）
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            {current ? (
              <div className="space-y-2">
                <p className="text-xs text-muted-foreground">
                  {current.submitted_by.display_name} 提交于{" "}
                  {formatDateTime(current.created_at)}
                </p>
                <DeliverableBody deliverable={current} />
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">加载中…</p>
            )}
          </section>

          {reviews && reviews.length > 0 && (
            <section className="space-y-2">
              <h3 className="text-sm font-medium text-muted-foreground">
                审核历史
              </h3>
              <ul className="space-y-2">
                {reviews.map((r) => (
                  <li key={r.id} className="space-y-1 rounded-md border p-3">
                    <div className="flex flex-wrap items-center gap-2">
                      <Badge
                        className={REVIEW_DECISION_META[r.decision].className}
                      >
                        {REVIEW_DECISION_META[r.decision].label}
                      </Badge>
                      <span className="text-xs text-muted-foreground">
                        针对第 {r.deliverable_version} 版 ·{" "}
                        {r.reviewed_by.display_name} ·{" "}
                        {formatDateTime(r.created_at)}
                      </span>
                    </div>
                    {r.feedback && (
                      <p className="whitespace-pre-wrap rounded-md bg-muted px-3 py-2 text-sm">
                        {r.feedback}
                      </p>
                    )}
                  </li>
                ))}
              </ul>
            </section>
          )}

          <section className="space-y-2">
            <Label>审核结论</Label>
            <div className="flex gap-2">
              {(Object.keys(REVIEW_DECISION_META) as ReviewDecision[]).map(
                (d) => (
                  <Button
                    key={d}
                    type="button"
                    size="sm"
                    variant={decision === d ? "default" : "outline"}
                    onClick={() => {
                      setDecision(d);
                      setFieldError(null);
                    }}
                  >
                    {REVIEW_DECISION_META[d].label}
                  </Button>
                ),
              )}
            </div>
          </section>

          <section className="space-y-2">
            <Label htmlFor="review-feedback">
              反馈{decision === "request_changes" ? "（必填）" : "（可选）"}
            </Label>
            <Textarea
              id="review-feedback"
              rows={4}
              placeholder="审核反馈仅负责人与该工作项主执行人可见（16 节）"
              value={feedback}
              onChange={(e) => setFeedback(e.target.value)}
            />
            {fieldError && (
              <p className="text-sm text-destructive">{fieldError}</p>
            )}
          </section>
        </div>

        <DialogFooter>
          <Button
            onClick={handleSubmit}
            disabled={review.isPending || !current}
            variant={decision === "reject" ? "destructive" : "default"}
          >
            {review.isPending ? "提交中…" : "提交审核结论"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
