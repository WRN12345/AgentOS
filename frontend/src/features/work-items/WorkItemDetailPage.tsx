import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Pencil } from "lucide-react";
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
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import {
  api,
  ApiError,
  errorMessage,
  newIdempotencyKey,
  VERSION_CONFLICT_MESSAGE,
} from "../../services/api";
import { useAuthStore, useIsLeader } from "../../app/store";
import type { Member, WorkItem, WorkItemStatus } from "../../types";
import { PRIORITY_META, STATUS_META, formatDateTime } from "./constants";
import { WorkItemFormDialog } from "./work-item-form";
import { DevDocSection } from "./DevDocSection";
import { CollaborationSection } from "../collaboration/CollaborationSection";
import { DeliverableSection } from "../deliverables/DeliverableSection";
import { TransferSection } from "../collaboration/TransferSection";
import { DeadlineChangeSection } from "../collaboration/DeadlineChangeSection";

/** 命令定义：状态机迁移动作（8.1 节）。 */
interface Command {
  key: string;
  label: string;
  path: string;
  /** 可见性：负责人动作或主执行人动作，叠加当前状态。 */
  visible: (ctx: {
    isLeader: boolean;
    isAssignee: boolean;
    status: WorkItemStatus;
  }) => boolean;
  variant?: "default" | "destructive" | "outline" | "secondary";
}

const COMMANDS: Command[] = [
  {
    key: "publish",
    label: "发布",
    path: "publish",
    visible: ({ isLeader, status }) => isLeader && status === "DRAFT",
  },
  {
    key: "start",
    label: "开始",
    path: "start",
    visible: ({ isAssignee, status }) => isAssignee && status === "READY",
  },
  {
    key: "block",
    label: "阻塞",
    path: "block",
    visible: ({ isAssignee, status }) => isAssignee && status === "IN_PROGRESS",
    variant: "outline",
  },
  {
    key: "unblock",
    label: "解除阻塞",
    path: "unblock",
    visible: ({ isAssignee, status }) => isAssignee && status === "BLOCKED",
  },
  {
    key: "submit",
    label: "提交审核",
    path: "submit",
    visible: ({ isAssignee, status }) => isAssignee && status === "IN_PROGRESS",
  },
  {
    key: "cancel",
    label: "取消",
    path: "cancel",
    visible: ({ isLeader, status }) =>
      isLeader && status !== "COMPLETED" && status !== "CANCELLED",
    variant: "destructive",
  },
];

/** 工作项详情页：完整字段、协作者、时间信息，以及按角色与状态显隐的命令按钮。 */
export default function WorkItemDetailPage() {
  const { id } = useParams<{ id: string }>();
  const queryClient = useQueryClient();
  const isLeader = useIsLeader();
  const selfMember = useAuthStore((s) => s.member);
  const [editOpen, setEditOpen] = useState(false);

  const { data: item, isLoading } = useQuery({
    queryKey: ["work-items", id],
    queryFn: () => api.get<WorkItem>(`/work-items/${id}`),
  });

  const { data: members } = useQuery({
    queryKey: ["members"],
    queryFn: () => api.get<Member[]>("/members"),
  });

  // 状态机命令：携带当前 version 与每次点击生成的幂等键
  const command = useMutation({
    mutationFn: (cmd: Command) =>
      api.post<WorkItem>(
        `/work-items/${id}/${cmd.path}`,
        { version: item!.version },
        newIdempotencyKey(),
      ),
    onSuccess: (_data, cmd) => {
      toast.success(`「${cmd.label}」操作成功`);
      queryClient.invalidateQueries({ queryKey: ["work-items"] });
    },
    onError: (error) => {
      // 开发文档前置：未确认文档且未豁免时 start 被 409 拦截，引导到文档区。
      // 注意：DEV_DOC_REQUIRED 也是 409，必须先于 isVersionConflict 判断。
      if (error instanceof ApiError && error.code === "DEV_DOC_REQUIRED") {
        toast.error(errorMessage(error, "请先提交开发文档并通过负责人确认"), {
          description: "可在下方「开发文档」区撰写并提交",
        });
        document
          .getElementById("dev-doc-section")
          ?.scrollIntoView({ behavior: "smooth", block: "start" });
        return;
      }
      if (error instanceof ApiError && error.isVersionConflict) {
        toast.error(VERSION_CONFLICT_MESSAGE);
        queryClient.invalidateQueries({ queryKey: ["work-items"] });
        return;
      }
      // T4.4：无交付物时提交审核被 422 拒绝，引导先提交交付物
      if (error instanceof ApiError && error.code === "DELIVERABLE_REQUIRED") {
        toast.error("请先提交交付物，再提交审核");
        return;
      }
      toast.error(errorMessage(error, "操作失败"));
    },
  });

  if (isLoading || !item) {
    return (
      <div className="space-y-2">
        <Skeleton className="h-8 w-64" />
        <Skeleton className="h-40 w-full" />
      </div>
    );
  }

  const isAssignee = selfMember?.id === item.assignee.id;
  const visibleCommands = COMMANDS.filter((c) =>
    c.visible({ isLeader, isAssignee, status: item.status }),
  );

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <Button variant="ghost" size="sm" asChild>
          <Link to="/work-items">
            <ArrowLeft className="size-4" />
            返回列表
          </Link>
        </Button>
        <div className="flex gap-2">
          {visibleCommands.map((cmd) => (
            <Button
              key={cmd.key}
              variant={cmd.variant ?? "default"}
              disabled={command.isPending}
              onClick={() => command.mutate(cmd)}
            >
              {cmd.label}
            </Button>
          ))}
          {isLeader && (
            <Button variant="outline" onClick={() => setEditOpen(true)}>
              <Pencil className="size-4" />
              编辑
            </Button>
          )}
        </div>
      </div>

      <Card>
        <CardHeader>
          <div className="flex items-center gap-2">
            <CardTitle>{item.title}</CardTitle>
            <Badge className={STATUS_META[item.status].className}>
              {STATUS_META[item.status].label}
            </Badge>
            <Badge className={PRIORITY_META[item.priority].className}>
              优先级：{PRIORITY_META[item.priority].label}
            </Badge>
          </div>
          <CardDescription>版本 v{item.version}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <section>
            <h3 className="mb-1 text-sm font-medium text-muted-foreground">
              说明
            </h3>
            <p className="whitespace-pre-wrap text-sm">
              {item.description || "（无）"}
            </p>
          </section>
          <Separator />
          <section>
            <h3 className="mb-1 text-sm font-medium text-muted-foreground">
              验收标准
            </h3>
            <p className="whitespace-pre-wrap text-sm">
              {item.acceptance_criteria || "（无）"}
            </p>
          </section>
          <Separator />
          <section className="grid grid-cols-2 gap-4 text-sm md:grid-cols-3">
            <div>
              <h3 className="mb-1 font-medium text-muted-foreground">
                主执行人
              </h3>
              {item.assignee.display_name}
            </div>
            <div>
              <h3 className="mb-1 font-medium text-muted-foreground">协作者</h3>
              {item.collaborators.length > 0 ? (
                <div className="flex flex-wrap gap-1">
                  {item.collaborators.map((c) => (
                    <Badge key={c.id} variant="secondary">
                      {c.display_name}
                    </Badge>
                  ))}
                </div>
              ) : (
                "（无）"
              )}
            </div>
            <div>
              <h3 className="mb-1 font-medium text-muted-foreground">
                截止时间
              </h3>
              {formatDateTime(item.due_at)}
            </div>
            <div>
              <h3 className="mb-1 font-medium text-muted-foreground">
                创建时间
              </h3>
              {formatDateTime(item.created_at)}
            </div>
            <div>
              <h3 className="mb-1 font-medium text-muted-foreground">
                更新时间
              </h3>
              {formatDateTime(item.updated_at)}
            </div>
          </section>
        </CardContent>
      </Card>

      <CollaborationSection workItem={item} members={members ?? []} />

      <DevDocSection workItem={item} />

      <DeliverableSection workItem={item} />

      <TransferSection workItem={item} members={members ?? []} />

      <DeadlineChangeSection workItem={item} />

      <WorkItemFormDialog
        open={editOpen}
        onOpenChange={setEditOpen}
        members={members ?? []}
        workItem={item}
      />
    </div>
  );
}
