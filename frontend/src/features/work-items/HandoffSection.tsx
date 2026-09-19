import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Form, FormControl, FormField, FormItem, FormLabel, FormMessage } from "@/components/ui/form";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { useAuthStore } from "../../app/store";
import { queryKeys } from "../../lib/queryKeys";
import { api, ApiError, errorMessage, newIdempotencyKey, VERSION_CONFLICT_MESSAGE } from "../../services/api";
import type { Deliverable, Handoff, HandoffTarget, WorkItem } from "../../types";
import { DeliverableBody } from "../deliverables/DeliverableBody";
import { formatDateTime } from "./constants";

export function useTaskHandoffs(workItemId: string) {
  return useQuery({
    queryKey: queryKeys.handoffs("task", workItemId),
    queryFn: () => api.get<Handoff[]>(`/work-items/${workItemId}/handoffs`),
    retry: false,
  });
}

const STATUS_LABEL = {
  pending: "待接收",
  accepted: "已接收",
  changes_requested: "待补正",
};

const sendSchema = z.object({
  targetId: z.string().min(1, "请选择接续任务"),
  note: z.string().max(10000, "移交说明最多 10000 字"),
});
const responseSchema = z.object({
  action: z.enum(["accept", "request-changes"]),
  note: z.string().max(10000, "接收反馈最多 10000 字"),
}).superRefine((values, ctx) => {
  if (values.action === "request-changes" && !values.note.trim()) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["note"], message: "请填写补正意见" });
  }
});
type SendValues = z.infer<typeof sendSchema>;
type ResponseValues = z.infer<typeof responseSchema>;

export function HandoffSection({ workItem }: { workItem: WorkItem }) {
  const member = useAuthStore((s) => s.member);
  const queryClient = useQueryClient();
  const history = useTaskHandoffs(workItem.id);
  const canSend = member?.id === workItem.assignee.id && workItem.status === "IN_PROGRESS";
  const form = useForm<SendValues>({
    resolver: zodResolver(sendSchema),
    defaultValues: { targetId: "", note: "" },
  });
  const targetId = form.watch("targetId");
  const deliveries = useQuery({
    queryKey: queryKeys.deliverables(workItem.id),
    queryFn: () => api.get<Deliverable[]>(`/work-items/${workItem.id}/deliverables`),
    enabled: canSend,
    retry: false,
  });
  const targets = useQuery({
    queryKey: queryKeys.handoffTargets(workItem.id),
    queryFn: () => api.get<HandoffTarget[]>(`/handoff-targets?source_work_item_id=${workItem.id}`),
    enabled: canSend,
  });
  const latest = deliveries.data?.[0];
  const target = targets.data?.find((item) => item.id === targetId);
  const refresh = () => {
    for (const key of [queryKeys.handoffs(), queryKeys.handoffTargets(), queryKeys.workItems(), queryKeys.deliverables(), queryKeys.members(), queryKeys.notifications(), queryKeys.auditEvents()]) {
      void queryClient.invalidateQueries({ queryKey: key });
    }
  };
  const onError = (error: unknown) => {
    if (error instanceof ApiError && error.isVersionConflict) {
      toast.error(VERSION_CONFLICT_MESSAGE);
      form.setValue("targetId", "");
      refresh();
      return;
    }
    toast.error(errorMessage(error, "移交操作失败"));
  };
  const send = useMutation({
    mutationFn: ({ note }: SendValues) => api.post<Handoff>(`/work-items/${workItem.id}/handoffs`, {
      version: workItem.version,
      target_work_item_id: target!.id,
      target_version: target!.version,
      deliverable_id: latest!.id,
      ...(note.trim() ? { note: note.trim() } : {}),
    }, newIdempotencyKey()),
    onSuccess: () => {
      toast.success("已移交，等待接收人确认");
      form.reset();
      refresh();
    },
    onError,
  });
  const sendDisabled = !target || !latest || send.isPending || targets.isFetching || targets.isError || deliveries.isFetching || deliveries.isError || history.isFetching || history.isError || history.data?.some((h) => h.status === "pending" && h.source_work_item.id === workItem.id);

  return (
    <Card id="handoff-section">
      <CardHeader>
        <CardTitle>任务移交</CardTitle>
        <CardDescription>接收人确认交付后，来源任务完成；接续任务由接收人手动开始。</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {canSend && (
          <div className="space-y-3 rounded-md border p-3">
            {deliveries.isError ? <p role="alert">交付物加载失败，请刷新后重试</p> : latest ? (
              <div className="space-y-2">
                <p className="text-sm font-medium">本次移交：交付物 v{latest.version}</p>
                <DeliverableBody deliverable={latest} />
              </div>
            ) : <p className="text-sm text-muted-foreground">{deliveries.isLoading ? "正在加载交付物…" : "请先提交交付物，再发起移交"}</p>}
            <Form {...form}>
              <form className="space-y-3" onSubmit={form.handleSubmit((values) => {
                if (!sendDisabled) send.mutate(values);
              })}>
                <FormField control={form.control} name="targetId" render={({ field }) => (
                  <FormItem>
                    <FormLabel>接续任务</FormLabel>
                    <Select value={field.value} onValueChange={field.onChange} disabled={send.isPending || targets.isFetching}>
                      <FormControl><SelectTrigger ref={field.ref} onBlur={field.onBlur}><SelectValue placeholder="选择待开始的接续任务" /></SelectTrigger></FormControl>
                      <SelectContent>
                        {(targets.data ?? []).map((item) => (
                          <SelectItem key={item.id} value={item.id}>{item.title} · {item.assignee.display_name}</SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                    <FormMessage />
                  </FormItem>
                )} />
                {targets.isError && <p role="alert">接续任务加载失败，请刷新后重试</p>}
                {targets.isSuccess && targets.data.length === 0 && <p className="text-sm text-muted-foreground">暂无其他成员负责的待开始任务</p>}
                {target && <p className="text-sm">接收人：{target.assignee.display_name} · 接续任务 v{target.version}</p>}
                <FormField control={form.control} name="note" render={({ field }) => (
                  <FormItem>
                    <FormLabel>移交说明（选填）</FormLabel>
                    <FormControl><Textarea {...field} maxLength={10000} disabled={send.isPending} /></FormControl>
                    <FormMessage />
                  </FormItem>
                )} />
                <Button type="submit" disabled={sendDisabled}>
                  {send.isPending ? "移交中…" : "确认移交"}
                </Button>
              </form>
            </Form>
          </div>
        )}
        {workItem.status === "WAITING_ACCEPTANCE" && <p className="text-sm text-muted-foreground">等待接收人确认，交付物与任务信息已锁定。</p>}
        {history.isError && <p role="alert">移交记录加载失败，请刷新后重试</p>}
        {history.isLoading && <p className="text-sm text-muted-foreground">正在加载移交记录…</p>}
        {history.isSuccess && history.data.length === 0 && <p className="text-sm text-muted-foreground">暂无移交记录</p>}
        {(history.data ?? []).map((handoff) => (
          <HandoffRecord key={handoff.id} handoff={handoff} memberId={member?.id} onChanged={refresh} onError={onError} />
        ))}
      </CardContent>
    </Card>
  );
}

function HandoffRecord({ handoff, memberId, onChanged, onError }: {
  handoff: Handoff;
  memberId?: string;
  onChanged: () => void;
  onError: (error: unknown) => void;
}) {
  const form = useForm<ResponseValues>({
    resolver: zodResolver(responseSchema),
    defaultValues: { action: "accept", note: "" },
  });
  const note = form.watch("note");
  const respond = useMutation({
    mutationFn: ({ action, note }: ResponseValues) => api.post<Handoff>(`/handoffs/${handoff.id}/${action}`, {
      version: handoff.version,
      ...(note.trim() ? { note: note.trim() } : {}),
    }, newIdempotencyKey()),
    onSuccess: (_result, { action }) => {
      toast.success(action === "accept" ? "已接收，来源任务已完成" : "已请求补正");
      form.reset();
      onChanged();
    },
    onError,
  });
  return (
    <section className="space-y-3 rounded-md border p-3" aria-label={`移交：${handoff.source_work_item.title}`}>
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant="outline">{STATUS_LABEL[handoff.status]}</Badge>
        <span className="text-sm">{handoff.sender.display_name} → {handoff.recipient.display_name}</span>
        <span className="text-xs text-muted-foreground">移交记录 v{handoff.version} · {formatDateTime(handoff.created_at)}</span>
      </div>
      <p className="text-sm">来源：{handoff.source_work_item.title} · v{handoff.source_work_item.version}</p>
      <p className="text-sm">接续：{handoff.target_work_item.title} · v{handoff.target_work_item.version}</p>
      <p className="text-sm font-medium">交付物 v{handoff.deliverable.version}</p>
      <DeliverableBody deliverable={handoff.deliverable} />
      {handoff.note && <p className="whitespace-pre-wrap text-sm">移交说明：{handoff.note}</p>}
      {handoff.response_note && <p className="whitespace-pre-wrap text-sm">接收反馈：{handoff.response_note}</p>}
      {handoff.responded_at && <p className="text-xs text-muted-foreground">处理时间：{formatDateTime(handoff.responded_at)}</p>}
      {handoff.status === "pending" && handoff.recipient.id === memberId && (
        <Form {...form}>
          <form className="space-y-2" onSubmit={form.handleSubmit((values) => {
            if (!respond.isPending) respond.mutate(values);
          })}>
            <FormField control={form.control} name="note" render={({ field }) => (
              <FormItem>
                <FormLabel>接收反馈（请求补正时必填）</FormLabel>
                <FormControl><Textarea {...field} maxLength={10000} disabled={respond.isPending} /></FormControl>
                <FormMessage />
              </FormItem>
            )} />
            <div className="flex flex-wrap gap-2">
              <Button type="submit" disabled={respond.isPending} onClick={() => form.setValue("action", "accept")}>确认接收</Button>
              <Button type="submit" variant="outline" disabled={respond.isPending || !note.trim()} onClick={() => form.setValue("action", "request-changes")}>请求补正</Button>
            </div>
          </form>
        </Form>
      )}
    </section>
  );
}
