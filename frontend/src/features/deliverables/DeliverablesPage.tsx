import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
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
import type { DeliverableListItem } from "../../types";
import { formatDateTime } from "../work-items/constants";
import { DELIVERABLE_TYPE_META, REVIEW_DECISION_META } from "./constants";

/**
 * 交付物聚合页：负责人/管理员见全部交付物，普通成员见相关工作项
 * （主执行人/协作者/协作请求任一方，16 节）的交付物及审核结论。
 * 提交新版本在任务详情页进行，最终审核在审批中心「交付审核」页签进行。
 */
export default function DeliverablesPage() {
  const { data: deliveries, isLoading } = useQuery({
    queryKey: ["deliverables", "visible"],
    queryFn: () => api.get<DeliverableListItem[]>("/deliverables"),
  });

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold">交付物</h1>
        <p className="text-sm text-muted-foreground">
          提交新版本在任务详情页进行；负责人最终审核在审批中心「交付审核」页签进行
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>交付记录</CardTitle>
          <CardDescription>
            按提交时间倒序；审核反馈仅负责人与任务主执行人可见
          </CardDescription>
        </CardHeader>
        <CardContent>
          {isLoading ? (
            <div className="space-y-2">
              <Skeleton className="h-10 w-full" />
              <Skeleton className="h-10 w-full" />
            </div>
          ) : !deliveries || deliveries.length === 0 ? (
            <p className="text-sm text-muted-foreground">暂无交付记录</p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>工作项</TableHead>
                  <TableHead>版本</TableHead>
                  <TableHead>提交人</TableHead>
                  <TableHead>审核结论</TableHead>
                  <TableHead>提交时间</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {deliveries.map((d) => (
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
                      第 {d.version} 版（{DELIVERABLE_TYPE_META[d.type].label}）
                    </TableCell>
                    <TableCell>{d.submitted_by.display_name}</TableCell>
                    <TableCell>
                      {d.review ? (
                        <div className="space-y-1">
                          <div className="flex items-center gap-2">
                            <Badge
                              className={
                                REVIEW_DECISION_META[d.review.decision]
                                  .className
                              }
                            >
                              {REVIEW_DECISION_META[d.review.decision].label}
                            </Badge>
                            <span className="text-xs text-muted-foreground">
                              {d.review.reviewed_by.display_name} ·{" "}
                              {formatDateTime(d.review.created_at)}
                            </span>
                          </div>
                          {d.review.feedback && (
                            <p className="whitespace-pre-wrap rounded-md bg-muted px-2 py-1 text-xs">
                              {d.review.feedback}
                            </p>
                          )}
                        </div>
                      ) : (
                        <span className="text-sm text-muted-foreground">
                          待审核
                        </span>
                      )}
                    </TableCell>
                    <TableCell>{formatDateTime(d.created_at)}</TableCell>
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
