import { useState } from "react";
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
import { Label } from "@/components/ui/label";
import { Progress } from "@/components/ui/progress";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Textarea } from "@/components/ui/textarea";
import { api, errorMessage } from "../../services/api";
import { useIsLeader } from "../../app/store";
import type { CoreMemoryEntry, CoreMemoryEntryList } from "../../types";
import { formatDateTime } from "../work-items/constants";
import { queryKeys } from "../../lib/queryKeys";

/**
 * 核心记忆页（M4.7，设计文档第 8 节）：
 * 项目成员可读条目列表（含来源：谁提的、谁确认的、何时生效）与容量占用；
 * 负责人可手写条目（种子记忆，立即生效）与作废条目。
 */
export default function CoreMemoryPage() {
  const queryClient = useQueryClient();
  const isLeader = useIsLeader();
  const [draft, setDraft] = useState("");

  const { data, isLoading } = useQuery({
    queryKey: queryKeys.coreMemory("entries"),
    queryFn: () => api.get<CoreMemoryEntryList>("/memory/core-entries"),
  });

  const createMutation = useMutation({
    mutationFn: (content: string) =>
      api.post<CoreMemoryEntry>("/memory/core-entries", { content }),
    onSuccess: () => {
      setDraft("");
      toast.success("核心记忆已添加并生效");
      void queryClient.invalidateQueries({ queryKey: queryKeys.coreMemory() });
    },
    onError: (error) => {
      toast.error(errorMessage(error, "添加失败"));
    },
  });

  const deprecateMutation = useMutation({
    mutationFn: (entryId: string) =>
      api.post<CoreMemoryEntry>(`/memory/core-entries/${entryId}/deprecate`),
    onSuccess: () => {
      toast.success("条目已作废（保留供追溯）");
      void queryClient.invalidateQueries({ queryKey: queryKeys.coreMemory() });
    },
    onError: (error) => {
      toast.error(errorMessage(error, "作废失败"));
    },
  });

  const entries = data?.entries ?? [];
  const used = data?.used_chars ?? 0;
  const budget = data?.budget_chars ?? 0;
  const usagePercent = budget > 0 ? Math.min(100, (used / budget) * 100) : 0;

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold">核心记忆</h1>
        <p className="text-sm text-muted-foreground">
          少量高价值条目：技术约定、关键决策、踩坑教训；每次 AI
          拆解/分配任务时全量注入参考
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">容量占用</CardTitle>
          <CardDescription>
            容量预算逼大家只留真正重要的；快满时请作废过时条目或确认整合精简提议
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-2">
          <Progress value={usagePercent} />
          <p className="text-sm text-muted-foreground">
            已用 {used} / {budget} 字符
          </p>
        </CardContent>
      </Card>

      {isLeader && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">手写条目</CardTitle>
            <CardDescription>
              负责人手写立即生效；新项目可先写几条种子记忆（技术栈、基本约定）
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="space-y-1.5">
              <Label htmlFor="core-memory-draft">内容</Label>
              <Textarea
                id="core-memory-draft"
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                placeholder="例如：本项目禁用递归查询"
                rows={3}
              />
            </div>
            <Button
              disabled={!draft.trim() || createMutation.isPending}
              onClick={() => createMutation.mutate(draft)}
            >
              添加并生效
            </Button>
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle className="text-base">条目列表</CardTitle>
          <CardDescription>
            生效在前；已作废条目保留供追溯，不再注入 AI 上下文
          </CardDescription>
        </CardHeader>
        <CardContent>
          {isLoading ? (
            <div className="space-y-2">
              <Skeleton className="h-10 w-full" />
              <Skeleton className="h-10 w-full" />
            </div>
          ) : entries.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              暂无核心记忆——本项目积累尚少
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>内容</TableHead>
                  <TableHead>状态</TableHead>
                  <TableHead>提议者</TableHead>
                  <TableHead>确认者</TableHead>
                  <TableHead>生效时间</TableHead>
                  {isLeader && <TableHead>操作</TableHead>}
                </TableRow>
              </TableHeader>
              <TableBody>
                {entries.map((entry) => (
                  <TableRow key={entry.id}>
                    <TableCell className="max-w-md whitespace-pre-wrap">
                      {entry.content}
                    </TableCell>
                    <TableCell>
                      {entry.status === "active" ? (
                        <Badge variant="default">生效中</Badge>
                      ) : (
                        <Badge variant="outline">已作废</Badge>
                      )}
                    </TableCell>
                    <TableCell>
                      {entry.proposed_by ? (
                        entry.proposed_by.display_name
                      ) : (
                        <span className="text-muted-foreground">AI 提议</span>
                      )}
                    </TableCell>
                    <TableCell>{entry.confirmed_by.display_name}</TableCell>
                    <TableCell>{formatDateTime(entry.effective_at)}</TableCell>
                    {isLeader && (
                      <TableCell>
                        {entry.status === "active" && (
                          <Button
                            variant="outline"
                            size="sm"
                            disabled={deprecateMutation.isPending}
                            onClick={() => deprecateMutation.mutate(entry.id)}
                          >
                            作废
                          </Button>
                        )}
                      </TableCell>
                    )}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
