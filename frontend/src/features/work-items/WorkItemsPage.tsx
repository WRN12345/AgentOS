import { useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Plus, Sparkles } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { api } from "../../services/api";
import { useIsLeader } from "../../app/store";
import type {
  AgentConfig,
  Member,
  WorkItemStatus,
  WorkItemSummary,
} from "../../types";
import { PRIORITY_META, STATUS_META, formatDate } from "./constants";
import { WorkItemFormDialog } from "./work-item-form";
import { RequirementGuidedCreateDialog } from "../agent-assistant/RequirementGuidedCreateDialog";

const STATUS_OPTIONS: WorkItemStatus[] = [
  "DRAFT",
  "READY",
  "IN_PROGRESS",
  "BLOCKED",
  "IN_REVIEW",
  "COMPLETED",
  "CANCELLED",
];

/** 工作项列表页：全员可见，支持按负责人、状态、DDL 区间过滤；负责人可创建。 */
export default function WorkItemsPage() {
  const isLeader = useIsLeader();
  const [createOpen, setCreateOpen] = useState(false);
  const [guideOpen, setGuideOpen] = useState(false);

  // 过滤条件（"all"/空串表示不过滤）
  const [assigneeId, setAssigneeId] = useState("all");
  const [status, setStatus] = useState("all");
  const [dueFrom, setDueFrom] = useState("");
  const [dueTo, setDueTo] = useState("");

  const { data: members } = useQuery({
    queryKey: ["members"],
    queryFn: () => api.get<Member[]>("/members"),
  });

  // 16 节：外部模型服务时引导对话框内提示"数据将发送至外部服务"
  const { data: config } = useQuery({
    queryKey: ["config"],
    queryFn: () => api.get<AgentConfig>("/config"),
    enabled: isLeader,
  });

  const params = new URLSearchParams();
  if (assigneeId !== "all") params.set("assignee_id", assigneeId);
  if (status !== "all") params.set("status", status);
  if (dueFrom) params.set("due_from", new Date(`${dueFrom}T00:00:00`).toISOString());
  if (dueTo) params.set("due_to", new Date(`${dueTo}T23:59:59`).toISOString());
  const queryString = params.toString();

  const { data: items, isLoading } = useQuery({
    queryKey: ["work-items", queryString],
    queryFn: () =>
      api.get<WorkItemSummary[]>(
        `/work-items${queryString ? `?${queryString}` : ""}`,
      ),
  });

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between space-y-0">
        <div>
          <CardTitle>工作项</CardTitle>
          <CardDescription>
            项目全部工作项的标题、状态、负责人、优先级与截止时间
          </CardDescription>
        </div>
        {isLeader && (
          <div className="flex gap-2">
            <Button variant="outline" onClick={() => setGuideOpen(true)}>
              <Sparkles className="size-4" />
              AI 需求引导
            </Button>
            <Button onClick={() => setCreateOpen(true)}>
              <Plus className="size-4" />
              创建工作项
            </Button>
          </div>
        )}
      </CardHeader>
      <CardContent className="space-y-4">
        {/* 过滤区 */}
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <div className="space-y-1">
            <Label>负责人</Label>
            <Select value={assigneeId} onValueChange={setAssigneeId}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">全部</SelectItem>
                {/* 管理员不参与工作协作，不作为主执行人筛选项 */}
                {(members ?? [])
                  .filter((m) => m.role !== "admin")
                  .map((m) => (
                    <SelectItem key={m.id} value={m.id}>
                      {m.display_name}
                    </SelectItem>
                  ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1">
            <Label>状态</Label>
            <Select value={status} onValueChange={setStatus}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">全部</SelectItem>
                {STATUS_OPTIONS.map((s) => (
                  <SelectItem key={s} value={s}>
                    {STATUS_META[s].label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1">
            <Label>DDL 从</Label>
            <Input
              type="date"
              value={dueFrom}
              onChange={(e) => setDueFrom(e.target.value)}
            />
          </div>
          <div className="space-y-1">
            <Label>DDL 至</Label>
            <Input
              type="date"
              value={dueTo}
              onChange={(e) => setDueTo(e.target.value)}
            />
          </div>
        </div>

        {isLoading ? (
          <div className="space-y-2">
            <Skeleton className="h-10 w-full" />
            <Skeleton className="h-10 w-full" />
            <Skeleton className="h-10 w-full" />
          </div>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>标题</TableHead>
                <TableHead>状态</TableHead>
                <TableHead>优先级</TableHead>
                <TableHead>主执行人</TableHead>
                <TableHead>截止时间</TableHead>
                <TableHead>更新时间</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {(items ?? []).map((item) => (
                <TableRow key={item.id}>
                  <TableCell>
                    <Link
                      to={`/work-items/${item.id}`}
                      className="font-medium text-primary hover:underline"
                    >
                      {item.title}
                    </Link>
                  </TableCell>
                  <TableCell>
                    <Badge className={STATUS_META[item.status].className}>
                      {STATUS_META[item.status].label}
                    </Badge>
                  </TableCell>
                  <TableCell>
                    <Badge className={PRIORITY_META[item.priority].className}>
                      {PRIORITY_META[item.priority].label}
                    </Badge>
                  </TableCell>
                  <TableCell>{item.assignee.display_name}</TableCell>
                  <TableCell>{formatDate(item.due_at)}</TableCell>
                  <TableCell>{formatDate(item.updated_at)}</TableCell>
                </TableRow>
              ))}
              {(items ?? []).length === 0 && (
                <TableRow>
                  <TableCell
                    colSpan={6}
                    className="text-center text-muted-foreground"
                  >
                    暂无符合条件的工作项
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        )}
      </CardContent>

      <WorkItemFormDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        members={members ?? []}
      />
      <RequirementGuidedCreateDialog
        open={guideOpen}
        onOpenChange={setGuideOpen}
        members={members ?? []}
        llmIsExternal={config?.llm_is_external ?? false}
      />
    </Card>
  );
}
