import { useState } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRightLeft } from "lucide-react";
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
import type { Member, TransferRequestSummary, WorkItem } from "../../types";
import { formatDateTime } from "../work-items/constants";
import { TRANSFER_STATUS_META } from "./constants";
import { queryKeys } from "../../lib/queryKeys";

const createSchema = z.object({
  to_member_id: z.string().min(1, "请选择新主执行人"),
  reason: z.string().min(1, "请输入转派原因"),
  impact_note: z.string().min(1, "请输入影响说明"),
});

type CreateValues = z.infer<typeof createSchema>;

interface Props {
  workItem: WorkItem;
  members: Member[];
}

/** 工作项详情页转派区（7.3 节）：主执行人申请转派，展示转派历史。 */
export function TransferSection({ workItem, members }: Props) {
  const queryClient = useQueryClient();
  const selfMember = useAuthStore((s) => s.member);
  const [createOpen, setCreateOpen] = useState(false);

  const { data: transfers } = useQuery({
    queryKey: queryKeys.transferRequests("work-item", workItem.id),
    queryFn: () =>
      api.get<TransferRequestSummary[]>(
        `/work-items/${workItem.id}/transfer-requests`,
      ),
  });

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: queryKeys.transferRequests() });
    queryClient.invalidateQueries({ queryKey: queryKeys.workItems() });
    queryClient.invalidateQueries({ queryKey: queryKeys.approvals() });
    queryClient.invalidateQueries({ queryKey: queryKeys.notifications() });
  };

  const form = useForm<CreateValues>({
    resolver: zodResolver(createSchema),
    defaultValues: { to_member_id: "", reason: "", impact_note: "" },
  });

  const createMutation = useMutation({
    mutationFn: (values: CreateValues) =>
      api.post(
        `/work-items/${workItem.id}/transfer-requests`,
        values,
        newIdempotencyKey(),
      ),
    onSuccess: () => {
      toast.success("转派申请已提交，待负责人审批");
      invalidate();
      form.reset();
      setCreateOpen(false);
    },
    onError: (error) => toast.error(errorMessage(error, "提交转派申请失败")),
  });

  // 发起人撤销待审批的转派申请
  const cancelMutation = useMutation({
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

  const isAssignee = selfMember?.id === workItem.assignee.id;
  const hasPending = (transfers ?? []).some((t) => t.status === "PENDING");
  const candidates = members.filter(
    (m) => m.is_active && m.id !== selfMember?.id,
  );

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between space-y-0">
        <div>
          <CardTitle>转派</CardTitle>
          <CardDescription>
            审批通过后主执行人才会变更，全程留痕（8.3 节）
          </CardDescription>
        </div>
        {isAssignee && (
          <Button
            size="sm"
            variant="outline"
            disabled={hasPending}
            title={hasPending ? "已存在待审批的转派申请" : undefined}
            onClick={() => setCreateOpen(true)}
          >
            <ArrowRightLeft className="size-4" />
            申请转派
          </Button>
        )}
      </CardHeader>
      <CardContent>
        {!transfers || transfers.length === 0 ? (
          <p className="text-sm text-muted-foreground">暂无转派记录</p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>原主执行人</TableHead>
                <TableHead>建议新主执行人</TableHead>
                <TableHead>状态</TableHead>
                <TableHead>申请时间</TableHead>
                <TableHead className="text-right">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {transfers.map((t) => (
                <TableRow key={t.id}>
                  <TableCell>{t.from_member.display_name}</TableCell>
                  <TableCell className="font-medium">
                    {t.to_member.display_name}
                  </TableCell>
                  <TableCell>
                    <Badge className={TRANSFER_STATUS_META[t.status].className}>
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
                          disabled={cancelMutation.isPending}
                          onClick={() => cancelMutation.mutate(t)}
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
            <DialogTitle>申请转派</DialogTitle>
            <DialogDescription>
              负责人审批通过前主执行人不变；原因与影响说明必填（7.3 节）。
            </DialogDescription>
          </DialogHeader>
          <Form {...form}>
            <form
              onSubmit={form.handleSubmit((v) => createMutation.mutate(v))}
              className="space-y-4"
            >
              <FormField
                control={form.control}
                name="to_member_id"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>建议新主执行人</FormLabel>
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
                control={form.control}
                name="reason"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>转派原因</FormLabel>
                    <FormControl>
                      <Textarea rows={3} {...field} />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <FormField
                control={form.control}
                name="impact_note"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>对 DDL 与现有协作的影响</FormLabel>
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
