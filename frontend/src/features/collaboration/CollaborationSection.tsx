import { useState } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
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
import {
  Form,
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from "@/components/ui/form";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
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
import {
  api,
  ApiError,
  errorMessage,
  newIdempotencyKey,
  VERSION_CONFLICT_MESSAGE,
} from "../../services/api";
import { useAuthStore } from "../../app/store";
import type {
  CollaborationRequest,
  CollaborationRequestSummary,
  Member,
  WorkItem,
} from "../../types";
import { formatDateTime } from "../work-items/constants";
import { COLLAB_STATUS_META } from "./constants";
import { queryKeys } from "../../lib/queryKeys";

const createSchema = z.object({
  assignee_id: z.string().min(1, "请选择接收人"),
  title: z.string().min(1, "请输入标题"),
  goal: z.string().min(1, "请输入协作目标"),
  template: z.string().optional(),
  due_at: z.string().optional(),
});

type CreateValues = z.infer<typeof createSchema>;

/** 将 <input type="datetime-local"> 的本地值转为 ISO 字符串（空值转为 null）。 */
function toIsoDateTime(value: string | undefined): string | null {
  if (!value) return null;
  return new Date(value).toISOString();
}

interface Props {
  workItem: WorkItem;
  members: Member[];
}

/** 工作项详情页协作区（13.2 节"我的协作"）：列表、发起、按身份与状态的状态机操作。 */
export function CollaborationSection({ workItem, members }: Props) {
  const queryClient = useQueryClient();
  const selfMember = useAuthStore((s) => s.member);
  const [createOpen, setCreateOpen] = useState(false);
  const [submitTarget, setSubmitTarget] =
    useState<CollaborationRequestSummary | null>(null);
  const [revisionTarget, setRevisionTarget] =
    useState<CollaborationRequestSummary | null>(null);
  const [detailTarget, setDetailTarget] =
    useState<CollaborationRequestSummary | null>(null);

  const { data: collabs } = useQuery({
    queryKey: queryKeys.collaborationRequests("work-item", workItem.id),
    queryFn: () =>
      api.get<CollaborationRequestSummary[]>(
        `/work-items/${workItem.id}/collaboration-requests`,
      ),
  });

  // 单条详情：列表摘要不含 goal/template/result_text，打开对话框时拉取
  const { data: detail } = useQuery({
    queryKey: queryKeys.collaborationRequests("detail", detailTarget?.id),
    queryFn: () =>
      api.get<CollaborationRequest>(
        `/collaboration-requests/${detailTarget!.id}`,
      ),
    enabled: detailTarget !== null,
  });

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: queryKeys.collaborationRequests() });
    queryClient.invalidateQueries({ queryKey: queryKeys.notifications() });
  };

  // 状态机命令（accept/decline/start/complete/cancel）：仅携带 version
  const command = useMutation({
    mutationFn: (vars: { collab: CollaborationRequestSummary; action: string }) =>
      api.post(
        `/collaboration-requests/${vars.collab.id}/${vars.action}`,
        { version: vars.collab.version },
        newIdempotencyKey(),
      ),
    onSuccess: (_data, vars) => {
      toast.success("操作成功");
      invalidate();
      void vars;
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

  const createForm = useForm<CreateValues>({
    resolver: zodResolver(createSchema),
    defaultValues: {
      assignee_id: "",
      title: "",
      goal: "",
      template: "",
      due_at: "",
    },
  });

  const createMutation = useMutation({
    mutationFn: (values: CreateValues) =>
      api.post(
        `/work-items/${workItem.id}/collaboration-requests`,
        {
          assignee_id: values.assignee_id,
          title: values.title,
          goal: values.goal,
          template: values.template || null,
          due_at: toIsoDateTime(values.due_at),
        },
        newIdempotencyKey(),
      ),
    onSuccess: () => {
      toast.success("协作请求已发起");
      invalidate();
      createForm.reset();
      setCreateOpen(false);
    },
    onError: (error) => toast.error(errorMessage(error, "发起协作请求失败")),
  });

  const submitForm = useForm<{ result_text: string }>({
    resolver: zodResolver(
      z.object({ result_text: z.string().min(1, "请输入回传内容") }),
    ),
    defaultValues: { result_text: "" },
  });

  const submitMutation = useMutation({
    mutationFn: (values: { result_text: string }) =>
      api.post(
        `/collaboration-requests/${submitTarget!.id}/submit`,
        { version: submitTarget!.version, result_text: values.result_text },
        newIdempotencyKey(),
      ),
    onSuccess: () => {
      toast.success("产物已回传");
      invalidate();
      submitForm.reset();
      setSubmitTarget(null);
    },
    onError: (error) => {
      if (error instanceof ApiError && error.isVersionConflict) {
        toast.error(VERSION_CONFLICT_MESSAGE);
        invalidate();
        return;
      }
      toast.error(errorMessage(error, "提交回传失败"));
    },
  });

  const revisionForm = useForm<{ feedback: string }>({
    defaultValues: { feedback: "" },
  });

  const revisionMutation = useMutation({
    mutationFn: (values: { feedback: string }) =>
      api.post(
        `/collaboration-requests/${revisionTarget!.id}/request-revision`,
        {
          version: revisionTarget!.version,
          feedback: values.feedback || null,
        },
        newIdempotencyKey(),
      ),
    onSuccess: () => {
      toast.success("已要求修改");
      invalidate();
      revisionForm.reset();
      setRevisionTarget(null);
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

  const isWorkItemAssignee = selfMember?.id === workItem.assignee.id;
  const candidates = members.filter(
    (m) => m.is_active && m.id !== selfMember?.id,
  );

  /** 按当前用户身份（发起人/接收人）与状态计算可见操作（8.2 节状态机）。 */
  const actionsFor = (c: CollaborationRequestSummary) => {
    const isRequester = selfMember?.id === c.requester.id;
    const isAssignee = selfMember?.id === c.assignee.id;
    const acts: { key: string; label: string; run: () => void; variant?: "default" | "outline" | "destructive" }[] = [];
    if (isAssignee && c.status === "REQUESTED") {
      acts.push(
        { key: "accept", label: "接受", run: () => command.mutate({ collab: c, action: "accept" }) },
        { key: "decline", label: "拒绝", run: () => command.mutate({ collab: c, action: "decline" }), variant: "outline" },
      );
    }
    if (isAssignee && (c.status === "ACCEPTED" || c.status === "REVISION_REQUESTED")) {
      acts.push({
        key: "start",
        label: c.status === "REVISION_REQUESTED" ? "继续处理" : "开始处理",
        run: () => command.mutate({ collab: c, action: "start" }),
      });
    }
    if (isAssignee && c.status === "IN_PROGRESS") {
      acts.push({ key: "submit", label: "提交回传", run: () => setSubmitTarget(c) });
    }
    if (isRequester && c.status === "SUBMITTED") {
      acts.push(
        { key: "complete", label: "完成", run: () => command.mutate({ collab: c, action: "complete" }) },
        { key: "revision", label: "要求修改", run: () => setRevisionTarget(c), variant: "outline" },
      );
    }
    if ((isRequester || isAssignee) && (c.status === "REQUESTED" || c.status === "ACCEPTED")) {
      acts.push({
        key: "cancel",
        label: "取消",
        run: () => command.mutate({ collab: c, action: "cancel" }),
        variant: "destructive",
      });
    }
    return acts;
  };

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between space-y-0">
        <div>
          <CardTitle>协作请求</CardTitle>
          <CardDescription>
            向其他成员索取资料、标注、评审或局部产物（不改变主执行人）
          </CardDescription>
        </div>
        {isWorkItemAssignee && (
          <Button size="sm" onClick={() => setCreateOpen(true)}>
            <Plus className="size-4" />
            发起协作
          </Button>
        )}
      </CardHeader>
      <CardContent>
        {!collabs || collabs.length === 0 ? (
          <p className="text-sm text-muted-foreground">暂无协作请求</p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>标题</TableHead>
                <TableHead>发起人</TableHead>
                <TableHead>接收人</TableHead>
                <TableHead>状态</TableHead>
                <TableHead>截止时间</TableHead>
                <TableHead className="text-right">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {collabs.map((c) => (
                <TableRow key={c.id}>
                  <TableCell className="font-medium">{c.title}</TableCell>
                  <TableCell>{c.requester.display_name}</TableCell>
                  <TableCell>{c.assignee.display_name}</TableCell>
                  <TableCell>
                    <Badge className={COLLAB_STATUS_META[c.status].className}>
                      {COLLAB_STATUS_META[c.status].label}
                    </Badge>
                  </TableCell>
                  <TableCell>{formatDateTime(c.due_at)}</TableCell>
                  <TableCell className="text-right">
                    <div className="flex justify-end gap-1">
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() => setDetailTarget(c)}
                      >
                        详情
                      </Button>
                      {actionsFor(c).map((a) => (
                        <Button
                          key={a.key}
                          size="sm"
                          variant={a.variant ?? "default"}
                          disabled={command.isPending}
                          onClick={a.run}
                        >
                          {a.label}
                        </Button>
                      ))}
                    </div>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>

      {/* 协作详情 */}
      <Dialog
        open={detailTarget !== null}
        onOpenChange={(open) => !open && setDetailTarget(null)}
      >
        <DialogContent className="max-h-[90vh] max-w-xl overflow-y-auto">
          <DialogHeader>
            <DialogTitle>协作请求详情</DialogTitle>
            <DialogDescription>
              {detailTarget?.work_item_title} · {detailTarget?.title}
            </DialogDescription>
          </DialogHeader>
          {!detail ? (
            <p className="text-sm text-muted-foreground">加载中…</p>
          ) : (
            <div className="space-y-4 text-sm">
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <h3 className="mb-1 font-medium text-muted-foreground">
                    发起人
                  </h3>
                  {detail.requester.display_name}
                </div>
                <div>
                  <h3 className="mb-1 font-medium text-muted-foreground">
                    接收人
                  </h3>
                  {detail.assignee.display_name}
                </div>
                <div>
                  <h3 className="mb-1 font-medium text-muted-foreground">
                    状态
                  </h3>
                  <Badge
                    className={COLLAB_STATUS_META[detail.status].className}
                  >
                    {COLLAB_STATUS_META[detail.status].label}
                  </Badge>
                </div>
                <div>
                  <h3 className="mb-1 font-medium text-muted-foreground">
                    截止时间
                  </h3>
                  {formatDateTime(detail.due_at)}
                </div>
              </div>
              <div>
                <h3 className="mb-1 font-medium text-muted-foreground">
                  协作目标
                </h3>
                <p className="whitespace-pre-wrap rounded-md bg-muted px-3 py-2">
                  {detail.goal}
                </p>
              </div>
              {detail.template && (
                <div>
                  <h3 className="mb-1 font-medium text-muted-foreground">
                    回传模板
                  </h3>
                  <p className="whitespace-pre-wrap rounded-md bg-muted px-3 py-2">
                    {detail.template}
                  </p>
                </div>
              )}
              {detail.result_text && (
                <div>
                  <h3 className="mb-1 font-medium text-muted-foreground">
                    回传产物
                  </h3>
                  <p className="whitespace-pre-wrap rounded-md bg-muted px-3 py-2">
                    {detail.result_text}
                  </p>
                </div>
              )}
            </div>
          )}
        </DialogContent>
      </Dialog>

      {/* 发起协作 */}
      <Dialog open={createOpen} onOpenChange={setCreateOpen}>
        <DialogContent className="max-h-[90vh] max-w-xl overflow-y-auto">
          <DialogHeader>
            <DialogTitle>发起协作请求</DialogTitle>
            <DialogDescription>
              协作请求有独立目标与截止时间，不影响主任务负责人。
            </DialogDescription>
          </DialogHeader>
          <Form {...createForm}>
            <form
              onSubmit={createForm.handleSubmit((v) => createMutation.mutate(v))}
              className="space-y-4"
            >
              <FormField
                control={createForm.control}
                name="assignee_id"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>接收人</FormLabel>
                    <Select value={field.value} onValueChange={field.onChange}>
                      <FormControl>
                        <SelectTrigger>
                          <SelectValue placeholder="选择成员" />
                        </SelectTrigger>
                      </FormControl>
                      <SelectContent>
                        {candidates.map((m) => (
                          <SelectItem key={m.id} value={m.id}>
                            {m.display_name}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <FormField
                control={createForm.control}
                name="title"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>标题</FormLabel>
                    <FormControl>
                      <Input {...field} />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <FormField
                control={createForm.control}
                name="goal"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>协作目标</FormLabel>
                    <FormControl>
                      <Textarea rows={3} {...field} />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <FormField
                control={createForm.control}
                name="template"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>回传模板（可选）</FormLabel>
                    <FormControl>
                      <Textarea
                        rows={3}
                        placeholder="约定回传内容的格式要求"
                        {...field}
                      />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <FormField
                control={createForm.control}
                name="due_at"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>截止时间（可选）</FormLabel>
                    <FormControl>
                      <Input type="datetime-local" {...field} />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <DialogFooter>
                <Button type="submit" disabled={createMutation.isPending}>
                  {createMutation.isPending ? "提交中…" : "发起"}
                </Button>
              </DialogFooter>
            </form>
          </Form>
        </DialogContent>
      </Dialog>

      {/* 提交回传 */}
      <Dialog
        open={submitTarget !== null}
        onOpenChange={(open) => !open && setSubmitTarget(null)}
      >
        <DialogContent className="max-w-xl">
          <DialogHeader>
            <DialogTitle>提交回传</DialogTitle>
            <DialogDescription>
              回传产物将发送给发起人「{submitTarget?.requester.display_name}」，
              并关联原工作项。
            </DialogDescription>
          </DialogHeader>
          <Form {...submitForm}>
            <form
              onSubmit={submitForm.handleSubmit((v) => submitMutation.mutate(v))}
              className="space-y-4"
            >
              <FormField
                control={submitForm.control}
                name="result_text"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>回传内容</FormLabel>
                    <FormControl>
                      <Textarea rows={5} {...field} />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <DialogFooter>
                <Button type="submit" disabled={submitMutation.isPending}>
                  {submitMutation.isPending ? "提交中…" : "提交"}
                </Button>
              </DialogFooter>
            </form>
          </Form>
        </DialogContent>
      </Dialog>

      {/* 要求修改 */}
      <Dialog
        open={revisionTarget !== null}
        onOpenChange={(open) => !open && setRevisionTarget(null)}
      >
        <DialogContent className="max-w-xl">
          <DialogHeader>
            <DialogTitle>要求修改</DialogTitle>
            <DialogDescription>
              回传的产物将退回给接收人继续处理。
            </DialogDescription>
          </DialogHeader>
          <Form {...revisionForm}>
            <form
              onSubmit={revisionForm.handleSubmit((v) =>
                revisionMutation.mutate(v),
              )}
              className="space-y-4"
            >
              <FormField
                control={revisionForm.control}
                name="feedback"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>修改意见（可选）</FormLabel>
                    <FormControl>
                      <Textarea rows={4} {...field} />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <DialogFooter>
                <Button type="submit" disabled={revisionMutation.isPending}>
                  {revisionMutation.isPending ? "提交中…" : "要求修改"}
                </Button>
              </DialogFooter>
            </form>
          </Form>
        </DialogContent>
      </Dialog>
    </Card>
  );
}
