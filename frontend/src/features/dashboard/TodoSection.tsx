import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Inbox } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { api } from "../../services/api";
import { useAuthStore } from "../../app/store";
import type {
  CollaborationRequestSummary,
  DeadlineChangeSummary,
  TransferRequestSummary,
  WorkItemSummary,
} from "../../types";

interface TodoEntry {
  key: string;
  category: string;
  categoryClassName: string;
  title: string;
  workItemId: string;
}

/** 待处理中心（13.2 节"待处理"）：由既有列表接口前端聚合当前用户需要动作的事项。 */
export function TodoSection() {
  const selfMember = useAuthStore((s) => s.member);

  const { data: received } = useQuery({
    queryKey: ["collaboration-requests", "received"],
    queryFn: () =>
      api.get<CollaborationRequestSummary[]>(
        "/collaboration-requests?role=received",
      ),
  });

  const { data: sent } = useQuery({
    queryKey: ["collaboration-requests", "sent"],
    queryFn: () =>
      api.get<CollaborationRequestSummary[]>(
        "/collaboration-requests?role=sent",
      ),
  });

  const { data: items } = useQuery({
    queryKey: ["work-items", ""],
    queryFn: () => api.get<WorkItemSummary[]>("/work-items"),
  });

  const { data: myTransfers } = useQuery({
    queryKey: ["transfer-requests", "mine"],
    queryFn: () =>
      api.get<TransferRequestSummary[]>("/transfer-requests?role=mine"),
  });

  const { data: myDeadlineChanges } = useQuery({
    queryKey: ["deadline-change-requests", "mine"],
    queryFn: () =>
      api.get<DeadlineChangeSummary[]>("/deadline-change-requests?role=mine"),
  });

  const todos: TodoEntry[] = [];

  // 收到的协作请求：待响应 / 待开始 / 进行中 / 待修改
  for (const c of received ?? []) {
    const base = {
      key: `collab-${c.id}`,
      title: `协作「${c.title}」（${c.work_item_title}）`,
      workItemId: c.work_item_id,
    };
    if (c.status === "REQUESTED") {
      todos.push({ ...base, category: "待响应", categoryClassName: "bg-blue-100 text-blue-700" });
    } else if (c.status === "ACCEPTED") {
      todos.push({ ...base, category: "待开始", categoryClassName: "bg-cyan-100 text-cyan-700" });
    } else if (c.status === "IN_PROGRESS") {
      todos.push({ ...base, category: "进行中", categoryClassName: "bg-amber-100 text-amber-700" });
    } else if (c.status === "REVISION_REQUESTED") {
      todos.push({ ...base, category: "待修改", categoryClassName: "bg-orange-100 text-orange-700" });
    }
  }

  // 发出的协作请求：已回传待我确认
  for (const c of sent ?? []) {
    if (c.status === "SUBMITTED") {
      todos.push({
        key: `collab-sent-${c.id}`,
        category: "待确认回传",
        categoryClassName: "bg-purple-100 text-purple-700",
        title: `协作「${c.title}」（${c.work_item_title}）`,
        workItemId: c.work_item_id,
      });
    }
  }

  // 我的 READY 工作项
  for (const item of items ?? []) {
    if (item.status === "READY" && item.assignee.id === selfMember?.id) {
      todos.push({
        key: `work-item-${item.id}`,
        category: "待开始任务",
        categoryClassName: "bg-blue-100 text-blue-700",
        title: `任务「${item.title}」`,
        workItemId: item.id,
      });
    }
  }

  // 我的申请审批进度
  for (const t of myTransfers ?? []) {
    if (t.status === "PENDING") {
      todos.push({
        key: `transfer-${t.id}`,
        category: "审批中",
        categoryClassName: "bg-muted text-muted-foreground",
        title: `转派申请（${t.work_item_title} → ${t.to_member.display_name}）`,
        workItemId: t.work_item_id,
      });
    }
  }
  for (const d of myDeadlineChanges ?? []) {
    if (
      d.status === "PENDING_IMPACT_ANALYSIS" ||
      d.status === "PENDING_APPROVAL"
    ) {
      todos.push({
        key: `deadline-${d.id}`,
        category: "审批中",
        categoryClassName: "bg-muted text-muted-foreground",
        title: `DDL 变更申请（${d.target_title}）`,
        workItemId: d.work_item_id,
      });
    }
  }

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center gap-2">
          <CardTitle>我的待处理</CardTitle>
          {todos.length > 0 && (
            <Badge variant="secondary">{todos.length}</Badge>
          )}
        </div>
        <CardDescription>
          需要接受、提交、修改或确认的事项，点击跳转对应任务
        </CardDescription>
      </CardHeader>
      <CardContent>
        {todos.length === 0 ? (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Inbox className="size-4" />
            暂无待处理事项
          </div>
        ) : (
          <ul className="space-y-2">
            {todos.map((todo) => (
              <li key={todo.key} className="flex items-center gap-2 text-sm">
                <Badge className={todo.categoryClassName}>
                  {todo.category}
                </Badge>
                <Link
                  to={`/work-items/${todo.workItemId}`}
                  className="truncate text-primary hover:underline"
                >
                  {todo.title}
                </Link>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
