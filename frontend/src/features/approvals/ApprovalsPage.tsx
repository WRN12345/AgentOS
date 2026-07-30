import { useState } from "react";
import { Link } from "react-router-dom";
import { useForm } from "react-hook-form";
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
import {
  Form,
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from "@/components/ui/form";
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/components/ui/tabs";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Textarea } from "@/components/ui/textarea";
import {
  api,
  ApiError,
  errorMessage,
  newIdempotencyKey,
  VERSION_CONFLICT_MESSAGE,
} from "../../services/api";
import { useAuthStore, useIsAdmin, useIsLeader } from "../../app/store";
import type {
  ApprovalItem,
  DeadlineChangeRequest,
  DeadlineChangeSummary,
  TransferRequest,
  TransferRequestSummary,
} from "../../types";
import { formatDateTime } from "../work-items/constants";
import {
  DEADLINE_CHANGE_STATUS_META,
  TRANSFER_STATUS_META,
} from "../collaboration/constants";
import { DeliveryReviewSection } from "./DeliveryReviewSection";

/** 审批中心（13.1 节）：负责人审批转派与主任务 DDL 变更并可查已处理记录；管理员只读待审批列表与审批记录；成员查看并撤销自己的申请。 */
export default function ApprovalsPage() {
  const isLeader = useIsLeader();
  const isAdmin = useIsAdmin();
  // 管理员可读待审批列表（无审批操作入口），交付审核仍为负责人专属
  const canSeePending = isLeader || isAdmin;

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold">审批中心</h1>
        <p className="text-sm text-muted-foreground">
          转派、DDL 变更与交付审核在这里集中处理
        </p>
      </div>
      <Tabs defaultValue={canSeePending ? "pending" : "mine"} className="space-y-4">
        <TabsList>
          {canSeePending && <TabsTrigger value="pending">待我审批</TabsTrigger>}
          {canSeePending && <TabsTrigger value="processed">审批记录</TabsTrigger>}
          {isLeader && <TabsTrigger value="delivery">交付审核</TabsTrigger>}
          <TabsTrigger value="mine">我的申请</TabsTrigger>
        </TabsList>
        {canSeePending && (
          <TabsContent value="pending">
            <PendingApprovals />
          </TabsContent>
        )}
        {canSeePending && (
          <TabsContent value="processed">
            <ProcessedApprovals />
          </TabsContent>
        )}
        {isLeader && (
          <TabsContent value="delivery">
            <DeliveryReviewSection />
          </TabsContent>
        )}
        <TabsContent value="mine">
          <MyRequests />
        </TabsContent>
      </Tabs>
    </div>
  );
}

/** 负责人待审批列表（GET /approvals）：转派与 DDL 变更统一卡片；管理员只读（无审批按钮）。 */
function PendingApprovals() {
  const queryClient = useQueryClient();
  const isLeader = useIsLeader();
  const [decision, setDecision] = useState<{
    item: ApprovalItem;
    action: "approve" | "reject";
  } | null>(null);
  const [detailItem, setDetailItem] = useState<ApprovalItem | null>(null);

  const { data: approvals, isLoading } = useQuery({
    queryKey: ["approvals"],
    queryFn: () => api.get<ApprovalItem[]>("/approvals"),
  });

  // 单条详情：审批聚合项不含 reason/impact_note/impact_analysis，打开对话框时拉取
  const { data: transferDetail } = useQuery({
    queryKey: ["transfer-requests", "detail", detailItem?.id],
    queryFn: () =>
      api.get<TransferRequest>(`/transfer-requests/${detailItem!.id}`),
    enabled: detailItem?.kind === "transfer",
  });
  const { data: deadlineDetail } = useQuery({
    queryKey: ["deadline-change-requests", "detail", detailItem?.id],
    queryFn: () =>
      api.get<DeadlineChangeRequest>(
        `/deadline-change-requests/${detailItem!.id}`,
      ),
    enabled: detailItem?.kind === "deadline_change",
  });

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["approvals"] });
    // 审批通过后该申请会进入「审批记录」列表，同步失效
    queryClient.invalidateQueries({ queryKey: ["approvals", "processed"] });
    queryClient.invalidateQueries({ queryKey: ["transfer-requests"] });
    queryClient.invalidateQueries({ queryKey: ["deadline-change-requests"] });
    queryClient.invalidateQueries({ queryKey: ["work-items"] });
    queryClient.invalidateQueries({ queryKey: ["notifications"] });
  };

  const decisionForm = useForm<{ decision_note: string }>({
    defaultValues: { decision_note: "" },
  });

  const decideMutation = useMutation({
    mutationFn: (values: { decision_note: string }) => {
      const { item, action } = decision!;
      const prefix =
        item.kind === "transfer"
          ? "/transfer-requests"
          : "/deadline-change-requests";
      return api.post(
        `${prefix}/${item.id}/${action}`,
        {
          version: item.version,
          decision_note: values.decision_note || null,
        },
        newIdempotencyKey(),
      );
    },
    onSuccess: (_data, values, _ctx) => {
      toast.success(
        decision?.action === "approve" ? "已通过审批" : "已驳回申请",
      );
      void values;
      invalidate();
      decisionForm.reset();
      setDecision(null);
    },
    onError: (error) => {
      if (error instanceof ApiError && error.isVersionConflict) {
        toast.error(VERSION_CONFLICT_MESSAGE);
        invalidate();
        return;
      }
      toast.error(errorMessage(error, "审批操作失败"));
    },
  });

  if (isLoading) {
    return <p className="text-sm text-muted-foreground">加载中…</p>;
  }

  if (!approvals || approvals.length === 0) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>待我审批</CardTitle>
          <CardDescription>当前没有待审批的转派或 DDL 变更申请</CardDescription>
        </CardHeader>
      </Card>
    );
  }

  return (
    <div className="space-y-3">
      {approvals.map((item) => (
        <Card key={`${item.kind}-${item.id}`}>
          <CardHeader>
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <Badge
                  variant={
                    item.kind === "transfer" ? "default" : "secondary"
                  }
                >
                  {item.kind === "transfer" ? "转派申请" : "DDL 变更"}
                </Badge>
                <Link
                  to={`/work-items/${item.work_item_id}`}
                  className="font-medium text-primary hover:underline"
                >
                  {item.work_item_title}
                </Link>
              </div>
              <span className="text-sm text-muted-foreground">
                {formatDateTime(item.created_at)}
              </span>
            </div>
            <CardDescription>{item.summary}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="grid grid-cols-2 gap-4 text-sm md:grid-cols-3">
              <div>
                <h3 className="mb-1 font-medium text-muted-foreground">
                  申请人
                </h3>
                {item.requested_by.display_name}
              </div>
              {item.kind === "transfer" && (
                <div>
                  <h3 className="mb-1 font-medium text-muted-foreground">
                    转派
                  </h3>
                  {item.from_member?.display_name} →{" "}
                  {item.to_member?.display_name}
                </div>
              )}
              {item.kind === "deadline_change" && (
                <>
                  <div>
                    <h3 className="mb-1 font-medium text-muted-foreground">
                      原截止时间
                    </h3>
                    {formatDateTime(item.old_due_at)}
                  </div>
                  <div>
                    <h3 className="mb-1 font-medium text-muted-foreground">
                      新截止时间
                    </h3>
                    {formatDateTime(item.new_due_at)}
                  </div>
                </>
              )}
            </div>
            {item.kind === "deadline_change" &&
              (item.impact_analysis_status === "unavailable" ? (
                <p className="rounded-md bg-amber-50 px-3 py-2 text-sm text-amber-700">
                  未生成 AI 影响分析，请基于业务信息人工决策（8.4 节）。
                </p>
              ) : (
                <p className="rounded-md bg-muted px-3 py-2 text-sm text-muted-foreground">
                  已生成规则化影响分析；AI 影响分析将在阶段 5 接入。
                </p>
              ))}
            <div className="flex gap-2">
              <Button
                size="sm"
                variant="outline"
                onClick={() => setDetailItem(item)}
              >
                查看详情
              </Button>
              {isLeader && (
                <>
                  <Button
                    size="sm"
                    onClick={() => setDecision({ item, action: "approve" })}
                  >
                    通过
                  </Button>
                  <Button
                    size="sm"
                    variant="destructive"
                    onClick={() => setDecision({ item, action: "reject" })}
                  >
                    驳回
                  </Button>
                </>
              )}
            </div>
          </CardContent>
        </Card>
      ))}

      {/* 申请详情对话框 */}
      <Dialog
        open={detailItem !== null}
        onOpenChange={(open) => !open && setDetailItem(null)}
      >
        <DialogContent className="max-h-[90vh] max-w-xl overflow-y-auto">
          <DialogHeader>
            <DialogTitle>
              {detailItem?.kind === "transfer" ? "转派申请详情" : "DDL 变更详情"}
            </DialogTitle>
            <DialogDescription>{detailItem?.summary}</DialogDescription>
          </DialogHeader>
          {detailItem?.kind === "transfer" &&
            (!transferDetail ? (
              <p className="text-sm text-muted-foreground">加载中…</p>
            ) : (
              <div className="space-y-4 text-sm">
                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <h3 className="mb-1 font-medium text-muted-foreground">
                      转派
                    </h3>
                    {transferDetail.from_member.display_name} →{" "}
                    {transferDetail.to_member.display_name}
                  </div>
                  <div>
                    <h3 className="mb-1 font-medium text-muted-foreground">
                      申请时间
                    </h3>
                    {formatDateTime(transferDetail.created_at)}
                  </div>
                </div>
                <div>
                  <h3 className="mb-1 font-medium text-muted-foreground">
                    申请原因
                  </h3>
                  <p className="whitespace-pre-wrap rounded-md bg-muted px-3 py-2">
                    {transferDetail.reason}
                  </p>
                </div>
                <div>
                  <h3 className="mb-1 font-medium text-muted-foreground">
                    影响说明
                  </h3>
                  <p className="whitespace-pre-wrap rounded-md bg-muted px-3 py-2">
                    {transferDetail.impact_note}
                  </p>
                </div>
              </div>
            ))}
          {detailItem?.kind === "deadline_change" &&
            (!deadlineDetail ? (
              <p className="text-sm text-muted-foreground">加载中…</p>
            ) : (
              <div className="space-y-4 text-sm">
                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <h3 className="mb-1 font-medium text-muted-foreground">
                      目标
                    </h3>
                    {deadlineDetail.target_type === "work_item"
                      ? "主任务："
                      : "协作："}
                    {deadlineDetail.target_title}
                  </div>
                  <div>
                    <h3 className="mb-1 font-medium text-muted-foreground">
                      申请人
                    </h3>
                    {deadlineDetail.requested_by.display_name}
                  </div>
                  <div>
                    <h3 className="mb-1 font-medium text-muted-foreground">
                      原截止时间
                    </h3>
                    {formatDateTime(deadlineDetail.old_due_at)}
                  </div>
                  <div>
                    <h3 className="mb-1 font-medium text-muted-foreground">
                      新截止时间
                    </h3>
                    {formatDateTime(deadlineDetail.new_due_at)}
                  </div>
                </div>
                <div>
                  <h3 className="mb-1 font-medium text-muted-foreground">
                    申请原因
                  </h3>
                  <p className="whitespace-pre-wrap rounded-md bg-muted px-3 py-2">
                    {deadlineDetail.reason}
                  </p>
                </div>
                {deadlineDetail.impact_analysis_status === "unavailable" ? (
                  <p className="rounded-md bg-amber-50 px-3 py-2 text-sm text-amber-700">
                    未生成 AI 影响分析
                  </p>
                ) : (
                  deadlineDetail.impact_analysis && (
                    <div className="space-y-3">
                      <h3 className="font-medium text-muted-foreground">
                        影响分析
                      </h3>
                      <div className="grid grid-cols-2 gap-4">
                        <div>
                          <h3 className="mb-1 font-medium text-muted-foreground">
                            主任务截止时间
                          </h3>
                          {formatDateTime(
                            deadlineDetail.impact_analysis.work_item.due_at,
                          )}
                        </div>
                        <div>
                          <h3 className="mb-1 font-medium text-muted-foreground">
                            是否晚于主任务 DDL
                          </h3>
                          {deadlineDetail.impact_analysis.exceeds_work_item_due
                            ? "是"
                            : "否"}
                        </div>
                      </div>
                      {deadlineDetail.impact_analysis
                        .affected_collaboration_requests.length > 0 && (
                        <div>
                          <h3 className="mb-1 font-medium text-muted-foreground">
                            受影响的协作请求
                          </h3>
                          <ul className="space-y-1">
                            {deadlineDetail.impact_analysis.affected_collaboration_requests.map(
                              (c) => (
                                <li
                                  key={c.id}
                                  className="rounded-md bg-muted px-3 py-2"
                                >
                                  {c.title}
                                  <span className="text-muted-foreground">
                                    （状态：{c.status}
                                    {c.due_at
                                      ? `，截止：${formatDateTime(c.due_at)}`
                                      : ""}
                                    ）
                                  </span>
                                </li>
                              ),
                            )}
                          </ul>
                        </div>
                      )}
                    </div>
                  )
                )}
              </div>
            ))}
        </DialogContent>
      </Dialog>

      {/* 审批意见对话框（通过/驳回共用） */}
      <Dialog
        open={decision !== null}
        onOpenChange={(open) => !open && setDecision(null)}
      >
        <DialogContent className="max-w-xl">
          <DialogHeader>
            <DialogTitle>
              {decision?.action === "approve" ? "通过申请" : "驳回申请"}
            </DialogTitle>
            <DialogDescription>
              {decision?.item.summary}。审批意见仅留痕审计，不进入通知正文。
            </DialogDescription>
          </DialogHeader>
          <Form {...decisionForm}>
            <form
              onSubmit={decisionForm.handleSubmit((v) =>
                decideMutation.mutate(v),
              )}
              className="space-y-4"
            >
              <FormField
                control={decisionForm.control}
                name="decision_note"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>审批意见（可选）</FormLabel>
                    <FormControl>
                      <Textarea rows={3} {...field} />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <DialogFooter>
                <Button
                  type="submit"
                  variant={
                    decision?.action === "reject" ? "destructive" : "default"
                  }
                  disabled={decideMutation.isPending}
                >
                  {decideMutation.isPending
                    ? "提交中…"
                    : decision?.action === "approve"
                      ? "确认通过"
                      : "确认驳回"}
                </Button>
              </DialogFooter>
            </form>
          </Form>
        </DialogContent>
      </Dialog>
    </div>
  );
}

/** 已处理审批记录（GET /approvals/processed，leader/admin 可见）：按处理时间倒序，只读。 */
function ProcessedApprovals() {
  const { data: processed, isLoading } = useQuery({
    queryKey: ["approvals", "processed"],
    queryFn: () => api.get<ApprovalItem[]>("/approvals/processed"),
  });

  if (isLoading) {
    return <p className="text-sm text-muted-foreground">加载中…</p>;
  }

  if (!processed || processed.length === 0) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>审批记录</CardTitle>
          <CardDescription>暂无审批记录</CardDescription>
        </CardHeader>
      </Card>
    );
  }

  return (
    <div className="space-y-3">
      {processed.map((item) => {
        const statusMeta =
          TRANSFER_STATUS_META[
            item.status as keyof typeof TRANSFER_STATUS_META
          ];
        return (
          <Card key={`${item.kind}-${item.id}`}>
            <CardHeader>
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <Badge
                    variant={
                      item.kind === "transfer" ? "default" : "secondary"
                    }
                  >
                    {item.kind === "transfer" ? "转派申请" : "DDL 变更"}
                  </Badge>
                  <Link
                    to={`/work-items/${item.work_item_id}`}
                    className="font-medium text-primary hover:underline"
                  >
                    {item.work_item_title}
                  </Link>
                  {statusMeta && (
                    <Badge className={statusMeta.className}>
                      {statusMeta.label}
                    </Badge>
                  )}
                </div>
                <span className="text-sm text-muted-foreground">
                  申请于 {formatDateTime(item.created_at)}
                </span>
              </div>
              <CardDescription>{item.summary}</CardDescription>
            </CardHeader>
            <CardContent>
              <div className="grid grid-cols-2 gap-4 text-sm md:grid-cols-3">
                <div>
                  <h3 className="mb-1 font-medium text-muted-foreground">
                    申请人
                  </h3>
                  {item.requested_by.display_name}
                </div>
                <div>
                  <h3 className="mb-1 font-medium text-muted-foreground">
                    处理人
                  </h3>
                  {item.approved_by?.display_name ?? "—"}
                </div>
                <div>
                  <h3 className="mb-1 font-medium text-muted-foreground">
                    处理时间
                  </h3>
                  {item.approved_at ? formatDateTime(item.approved_at) : "—"}
                </div>
              </div>
            </CardContent>
          </Card>
        );
      })}
    </div>
  );
}

/** 成员侧"我的申请"：我发起的转派与 DDL 变更及审批进度，待审批可撤销。 */
function MyRequests() {
  const queryClient = useQueryClient();
  const selfMember = useAuthStore((s) => s.member);

  const { data: transfers } = useQuery({
    queryKey: ["transfer-requests", "mine"],
    queryFn: () =>
      api.get<TransferRequestSummary[]>("/transfer-requests?role=mine"),
  });

  const { data: deadlineChanges } = useQuery({
    queryKey: ["deadline-change-requests", "mine"],
    queryFn: () =>
      api.get<DeadlineChangeSummary[]>("/deadline-change-requests?role=mine"),
  });

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["transfer-requests"] });
    queryClient.invalidateQueries({ queryKey: ["deadline-change-requests"] });
    queryClient.invalidateQueries({ queryKey: ["approvals"] });
    queryClient.invalidateQueries({ queryKey: ["notifications"] });
  };

  const cancelTransfer = useMutation({
    mutationFn: (t: TransferRequestSummary) =>
      api.post(
        `/transfer-requests/${t.id}/cancel`,
        { version: t.version },
        newIdempotencyKey(),
      ),
    onSuccess: () => {
      toast.success("转派申请已取消");
      invalidate();
    },
    onError: (error) => {
      if (error instanceof ApiError && error.isVersionConflict) {
        toast.error(VERSION_CONFLICT_MESSAGE);
        invalidate();
        return;
      }
      toast.error(errorMessage(error, "操作失败"));
    },
  });

  const cancelDeadlineChange = useMutation({
    mutationFn: (d: DeadlineChangeSummary) =>
      api.post(
        `/deadline-change-requests/${d.id}/cancel`,
        { version: d.version },
        newIdempotencyKey(),
      ),
    onSuccess: () => {
      toast.success("DDL 变更申请已取消");
      invalidate();
    },
    onError: (error) => {
      if (error instanceof ApiError && error.isVersionConflict) {
        toast.error(VERSION_CONFLICT_MESSAGE);
        invalidate();
        return;
      }
      toast.error(errorMessage(error, "操作失败"));
    },
  });

  const empty =
    (transfers ?? []).length === 0 && (deadlineChanges ?? []).length === 0;

  if (empty) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>我的申请</CardTitle>
          <CardDescription>
            我发起的转派与 DDL 变更申请会显示在这里
          </CardDescription>
        </CardHeader>
      </Card>
    );
  }

  return (
    <div className="space-y-4">
      {(transfers ?? []).length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle>我的转派申请</CardTitle>
          </CardHeader>
          <CardContent>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>工作项</TableHead>
                  <TableHead>建议新主执行人</TableHead>
                  <TableHead>状态</TableHead>
                  <TableHead>申请时间</TableHead>
                  <TableHead className="text-right">操作</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {(transfers ?? []).map((t) => (
                  <TableRow key={t.id}>
                    <TableCell>
                      <Link
                        to={`/work-items/${t.work_item_id}`}
                        className="font-medium text-primary hover:underline"
                      >
                        {t.work_item_title}
                      </Link>
                    </TableCell>
                    <TableCell>{t.to_member.display_name}</TableCell>
                    <TableCell>
                      <Badge
                        className={TRANSFER_STATUS_META[t.status].className}
                      >
                        {TRANSFER_STATUS_META[t.status].label}
                      </Badge>
                    </TableCell>
                    <TableCell>{formatDateTime(t.created_at)}</TableCell>
                    <TableCell className="text-right">
                      {t.status === "PENDING" &&
                        selfMember?.id === t.from_member.id && (
                          <Button
                            size="sm"
                            variant="destructive"
                            disabled={cancelTransfer.isPending}
                            onClick={() => cancelTransfer.mutate(t)}
                          >
                            取消申请
                          </Button>
                        )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      )}

      {(deadlineChanges ?? []).length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle>我的 DDL 变更申请</CardTitle>
          </CardHeader>
          <CardContent>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>工作项</TableHead>
                  <TableHead>目标</TableHead>
                  <TableHead>新截止时间</TableHead>
                  <TableHead>状态</TableHead>
                  <TableHead>申请时间</TableHead>
                  <TableHead className="text-right">操作</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {(deadlineChanges ?? []).map((d) => (
                  <TableRow key={d.id}>
                    <TableCell>
                      <Link
                        to={`/work-items/${d.work_item_id}`}
                        className="font-medium text-primary hover:underline"
                      >
                        {d.work_item_title}
                      </Link>
                    </TableCell>
                    <TableCell>
                      {d.target_type === "work_item" ? "主任务：" : "协作："}
                      {d.target_title}
                    </TableCell>
                    <TableCell>{formatDateTime(d.new_due_at)}</TableCell>
                    <TableCell>
                      <Badge
                        className={
                          DEADLINE_CHANGE_STATUS_META[d.status].className
                        }
                      >
                        {DEADLINE_CHANGE_STATUS_META[d.status].label}
                      </Badge>
                    </TableCell>
                    <TableCell>{formatDateTime(d.created_at)}</TableCell>
                    <TableCell className="text-right">
                      {(d.status === "PENDING_IMPACT_ANALYSIS" ||
                        d.status === "PENDING_APPROVAL") &&
                        selfMember?.id === d.requested_by.id && (
                          <Button
                            size="sm"
                            variant="destructive"
                            disabled={cancelDeadlineChange.isPending}
                            onClick={() => cancelDeadlineChange.mutate(d)}
                          >
                            取消申请
                          </Button>
                        )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
