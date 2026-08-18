import { useState } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CalendarClock } from "lucide-react";
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
import { useAuthStore, useIsLeader } from "../../app/store";
import type {
  CollaborationRequestSummary,
  DeadlineChangeSummary,
  WorkItem,
} from "../../types";
import { formatDateTime } from "../work-items/constants";
import { DEADLINE_CHANGE_STATUS_META } from "./constants";
import { queryKeys } from "../../lib/queryKeys";

const createSchema = z.object({
  target: z.string().min(1, "请选择变更目标"),
  new_due_at: z.string().min(1, "请选择新截止时间"),
  reason: z.string().min(1, "请输入变更原因"),
});

type CreateValues = z.infer<typeof createSchema>;

/** 协作请求的可申请状态（进行中链路内的协作才有 DDL 协商意义）。 */
const ACTIVE_COLLAB_STATUSES = new Set([
  "REQUESTED",
  "ACCEPTED",
  "IN_PROGRESS",
  "REVISION_REQUESTED",
]);

interface Props {
  workItem: WorkItem;
}

/** 工作项详情页 DDL 变更区（7.4 节）：主任务级需负责人审批，协作级由双方协商。 */
export function DeadlineChangeSection({ workItem }: Props) {
  const queryClient = useQueryClient();
  const selfMember = useAuthStore((s) => s.member);
  const isLeader = useIsLeader();
  const [createOpen, setCreateOpen] = useState(false);

  const { data: changes } = useQuery({
    queryKey: queryKeys.deadlineChangeRequests("work-item", workItem.id),
    queryFn: () =>
      api.get<DeadlineChangeSummary[]>(
        `/work-items/${workItem.id}/deadline-change-requests`,
      ),
  });

  // 协作级 DDL 变更需要关联协作请求：复用协作区同一查询缓存
  const { data: collabs } = useQuery({
    queryKey: queryKeys.collaborationRequests("work-item", workItem.id),
    queryFn: () =>
      api.get<CollaborationRequestSummary[]>(
        `/work-items/${workItem.id}/collaboration-requests`,
      ),
  });

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: queryKeys.deadlineChangeRequests() });
    queryClient.invalidateQueries({ queryKey: queryKeys.collaborationRequests() });
    queryClient.invalidateQueries({ queryKey: queryKeys.workItems() });
    queryClient.invalidateQueries({ queryKey: queryKeys.approvals() });
    queryClient.invalidateQueries({ queryKey: queryKeys.notifications() });
  };

  const form = useForm<CreateValues>({
    resolver: zodResolver(createSchema),
    defaultValues: { target: "", new_due_at: "", reason: "" },
  });

  const createMutation = useMutation({
    mutationFn: (values: CreateValues) => {
      const [targetType, targetId] = values.target.split(":");
      return api.post(
        `/work-items/${workItem.id}/deadline-change-requests`,
        {
          target_type: targetType,
          target_id: targetId,
          new_due_at: new Date(values.new_due_at).toISOString(),
          reason: values.reason,
        },
        newIdempotencyKey(),
      );
    },
    onSuccess: () => {
      toast.success("DDL 变更申请已提交");
      invalidate();
      form.reset();
      setCreateOpen(false);
    },
    onError: (error) => toast.error(errorMessage(error, "提交 DDL 变更申请失败")),
  });

  const cancelMutation = useMutation({
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

  // 可选目标：主任务级（主执行人或负责人）+ 我参与的进行中协作（协作双方）
  const isAssignee = selfMember?.id === workItem.assignee.id;
  const targetOptions: { value: string; label: string }[] = [];
  if (isAssignee || isLeader) {
    targetOptions.push({
      value: `work_item:${workItem.id}`,
      label: `主任务 DDL（${workItem.title}）`,
    });
  }
  for (const c of collabs ?? []) {
    const involved =
      selfMember?.id === c.requester.id || selfMember?.id === c.assignee.id;
    if (involved && ACTIVE_COLLAB_STATUSES.has(c.status)) {
      targetOptions.push({
        value: `collaboration_request:${c.id}`,
        label: `协作 DDL（${c.title}）`,
      });
    }
  }

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between space-y-0">
        <div>
          <CardTitle>DDL 变更</CardTitle>
          <CardDescription>
            主任务 DDL 变更须负责人审批；协作 DDL 不影响主任务时双方确认即生效（7.4
            节）
          </CardDescription>
        </div>
        {targetOptions.length > 0 && (
          <Button
            size="sm"
            variant="outline"
            onClick={() => setCreateOpen(true)}
          >
            <CalendarClock className="size-4" />
            申请变更
          </Button>
        )}
      </CardHeader>
      <CardContent>
        {!changes || changes.length === 0 ? (
          <p className="text-sm text-muted-foreground">暂无 DDL 变更记录</p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>目标</TableHead>
                <TableHead>原截止时间</TableHead>
                <TableHead>新截止时间</TableHead>
                <TableHead>申请人</TableHead>
                <TableHead>状态</TableHead>
                <TableHead className="text-right">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {changes.map((d) => (
                <TableRow key={d.id}>
                  <TableCell className="font-medium">
                    {d.target_type === "work_item" ? "主任务：" : "协作："}
                    {d.target_title}
                  </TableCell>
                  <TableCell>{formatDateTime(d.old_due_at)}</TableCell>
                  <TableCell>{formatDateTime(d.new_due_at)}</TableCell>
                  <TableCell>{d.requested_by.display_name}</TableCell>
                  <TableCell>
                    <Badge
                      className={
                        DEADLINE_CHANGE_STATUS_META[d.status].className
                      }
                    >
                      {DEADLINE_CHANGE_STATUS_META[d.status].label}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-right">
                    {(d.status === "PENDING_IMPACT_ANALYSIS" ||
                      d.status === "PENDING_APPROVAL") &&
                      selfMember?.id === d.requested_by.id && (
                        <Button
                          size="sm"
                          variant="destructive"
                          disabled={cancelMutation.isPending}
                          onClick={() => cancelMutation.mutate(d)}
                        >
                          取消申请
                        </Button>
                      )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>

      <Dialog open={createOpen} onOpenChange={setCreateOpen}>
        <DialogContent className="max-w-xl">
          <DialogHeader>
            <DialogTitle>申请 DDL 变更</DialogTitle>
            <DialogDescription>
              主任务 DDL 的任何修改都必须由项目负责人批准。
            </DialogDescription>
          </DialogHeader>
          <Form {...form}>
            <form
              onSubmit={form.handleSubmit((v) => createMutation.mutate(v))}
              className="space-y-4"
            >
              <FormField
                control={form.control}
                name="target"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>变更目标</FormLabel>
                    <Select value={field.value} onValueChange={field.onChange}>
                      <FormControl>
                        <SelectTrigger>
                          <SelectValue placeholder="选择目标" />
                        </SelectTrigger>
                      </FormControl>
                      <SelectContent>
                        {targetOptions.map((o) => (
                          <SelectItem key={o.value} value={o.value}>
                            {o.label}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <FormField
                control={form.control}
                name="new_due_at"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>新截止时间</FormLabel>
                    <FormControl>
                      <Input type="datetime-local" {...field} />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <FormField
                control={form.control}
                name="reason"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>变更原因</FormLabel>
                    <FormControl>
                      <Textarea rows={3} {...field} />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <DialogFooter>
                <Button type="submit" disabled={createMutation.isPending}>
                  {createMutation.isPending ? "提交中…" : "提交申请"}
                </Button>
              </DialogFooter>
            </form>
          </Form>
        </DialogContent>
      </Dialog>
    </Card>
  );
}
