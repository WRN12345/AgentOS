import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus } from "lucide-react";
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
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import {
  api,
  ApiError,
  errorMessage,
  newIdempotencyKey,
} from "../../services/api";
import { useAuthStore } from "../../app/store";
import type {
  Deliverable,
  DeliverableType,
  Review,
  StoredFile,
  WorkItem,
} from "../../types";
import { formatDateTime } from "../work-items/constants";
import {
  DELIVERABLE_TYPE_META,
  REVIEW_DECISION_META,
} from "./constants";
import { DeliverableBody } from "./DeliverableBody";
import { FileUploadField } from "./FileUploadField";

interface Props {
  workItem: WorkItem;
}

/**
 * 工作项详情页交付区（13.2 节）：版本历史（含哈希/提交人/时间）、
 * 三类交付物提交 Dialog、审核反馈区（仅负责人与主执行人可见）。
 * 交付物列表对工作项无关成员 403，整体静默不渲染。
 */
export function DeliverableSection({ workItem }: Props) {
  const queryClient = useQueryClient();
  const selfMember = useAuthStore((s) => s.member);
  const [submitOpen, setSubmitOpen] = useState(false);

  const { data: deliverables, isError: deliverablesForbidden } = useQuery({
    queryKey: ["deliverables", workItem.id],
    queryFn: () =>
      api.get<Deliverable[]>(`/work-items/${workItem.id}/deliverables`),
    retry: false,
  });

  // 审核反馈仅负责人与主执行人可见（16 节），其余 403 时静默不渲染
  const { data: reviews } = useQuery({
    queryKey: ["reviews", workItem.id],
    queryFn: () => api.get<Review[]>(`/work-items/${workItem.id}/reviews`),
    retry: false,
  });

  if (deliverablesForbidden) {
    return null;
  }

  const isAssignee = selfMember?.id === workItem.assignee.id;
  const terminal =
    workItem.status === "COMPLETED" || workItem.status === "CANCELLED";
  const canSubmit = isAssignee && !terminal;
  const latest = deliverables?.[0];

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <div>
            <CardTitle>交付物</CardTitle>
            <CardDescription>
              {latest
                ? `当前第 ${latest.version} 版，每次提交生成新版本，历史版本保留可查`
                : "尚未提交交付物"}
            </CardDescription>
          </div>
          {canSubmit && (
            <Button size="sm" onClick={() => setSubmitOpen(true)}>
              <Plus className="size-4" />
              提交交付
            </Button>
          )}
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        {deliverables && deliverables.length > 0 ? (
          <ul className="space-y-3">
            {deliverables.map((d) => (
              <li key={d.id} className="space-y-2 rounded-md border p-3">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge variant="outline">v{d.version}</Badge>
                  <Badge className={DELIVERABLE_TYPE_META[d.type].className}>
                    {DELIVERABLE_TYPE_META[d.type].label}
                  </Badge>
                  <span className="text-xs text-muted-foreground">
                    {d.submitted_by.display_name} 提交于{" "}
                    {formatDateTime(d.created_at)}
                  </span>
                </div>
                <DeliverableBody deliverable={d} />
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-sm text-muted-foreground">
            暂无交付记录。提交审核前需先提交交付物。
          </p>
        )}

        {reviews && reviews.length > 0 && (
          <section className="space-y-2">
            <h3 className="text-sm font-medium text-muted-foreground">
              审核反馈（仅负责人与主执行人可见）
            </h3>
            <ul className="space-y-2">
              {reviews.map((r) => (
                <li key={r.id} className="space-y-1 rounded-md border p-3">
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge className={REVIEW_DECISION_META[r.decision].className}>
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
      </CardContent>

      {canSubmit && (
        <SubmitDeliverableDialog
          workItemId={workItem.id}
          open={submitOpen}
          onOpenChange={setSubmitOpen}
          onSubmitted={() => {
            queryClient.invalidateQueries({
              queryKey: ["deliverables", workItem.id],
            });
            queryClient.invalidateQueries({ queryKey: ["work-items"] });
            queryClient.invalidateQueries({ queryKey: ["notifications"] });
          }}
        />
      )}
    </Card>
  );
}

interface DialogProps {
  workItemId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSubmitted: () => void;
}

/** 提交交付 Dialog：三种类型切换（Git 链接 / 文本 / 文件上传）。 */
function SubmitDeliverableDialog({
  workItemId,
  open,
  onOpenChange,
  onSubmitted,
}: DialogProps) {
  const [type, setType] = useState<DeliverableType>("git_link");
  const [content, setContent] = useState("");
  const [uploaded, setUploaded] = useState<StoredFile | null>(null);
  const [fieldError, setFieldError] = useState<string | null>(null);

  const reset = () => {
    setType("git_link");
    setContent("");
    setUploaded(null);
    setFieldError(null);
  };

  const submit = useMutation({
    mutationFn: () => {
      const body =
        type === "file"
          ? { type, file_id: uploaded!.id }
          : { type, content: content.trim() };
      return api.post<Deliverable>(
        `/work-items/${workItemId}/deliverables`,
        body,
        newIdempotencyKey(),
      );
    },
    onSuccess: (d) => {
      toast.success(`交付物已提交（第 ${d.version} 版）`);
      onSubmitted();
      onOpenChange(false);
      reset();
    },
    onError: (error) => {
      if (error instanceof ApiError && error.isVersionConflict) {
        toast.error("交付物版本冲突，请刷新后重试");
        onSubmitted();
        return;
      }
      toast.error(errorMessage(error, "提交失败"));
    },
  });

  const handleSubmit = () => {
    if (type === "file") {
      if (!uploaded) {
        setFieldError("请先选择并上传文件");
        return;
      }
    } else if (!content.trim()) {
      setFieldError(
        type === "git_link" ? "请输入 Git 链接" : "请输入文本说明",
      );
      return;
    }
    setFieldError(null);
    submit.mutate();
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(v) => {
        onOpenChange(v);
        if (!v) reset();
      }}
    >
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>提交交付</DialogTitle>
          <DialogDescription>
            每次提交生成一个新版本，旧版本保留可查（7.5 节）。
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <div className="space-y-2">
            <Label>交付形式</Label>
            <Select
              value={type}
              onValueChange={(v) => {
                setType(v as DeliverableType);
                setFieldError(null);
              }}
            >
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="git_link">Git 链接</SelectItem>
                <SelectItem value="text">文本说明</SelectItem>
                <SelectItem value="file">文件上传</SelectItem>
              </SelectContent>
            </Select>
          </div>

          {type === "git_link" && (
            <div className="space-y-2">
              <Label htmlFor="deliverable-git-link">Git 链接</Label>
              <Input
                id="deliverable-git-link"
                placeholder="https://github.com/org/repo/pull/123"
                value={content}
                onChange={(e) => setContent(e.target.value)}
              />
            </div>
          )}

          {type === "text" && (
            <div className="space-y-2">
              <Label htmlFor="deliverable-text">文本说明</Label>
              <Textarea
                id="deliverable-text"
                rows={5}
                placeholder="交付内容说明…"
                value={content}
                onChange={(e) => setContent(e.target.value)}
              />
            </div>
          )}

          {type === "file" && (
            <div className="space-y-2">
              <Label>文件</Label>
              <FileUploadField
                workItemId={workItemId}
                onUploaded={(f) => {
                  setUploaded(f);
                  setFieldError(null);
                }}
                onClear={() => setUploaded(null)}
              />
            </div>
          )}

          {fieldError && (
            <p className="text-sm text-destructive">{fieldError}</p>
          )}
        </div>
        <DialogFooter>
          <Button
            onClick={handleSubmit}
            disabled={submit.isPending || (type === "file" && !uploaded)}
          >
            {submit.isPending ? "提交中…" : "提交"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
