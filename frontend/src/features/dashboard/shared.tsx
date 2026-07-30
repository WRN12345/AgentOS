import { Link } from "react-router-dom";
import {
  Card,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { cn } from "@/lib/utils";
import type { WorkItemStatus } from "../../types";

/** 未完成任务状态（工作台与团队概览共用）。 */
export const ACTIVE_STATUSES: WorkItemStatus[] = [
  "READY",
  "IN_PROGRESS",
  "BLOCKED",
  "IN_REVIEW",
];

/** 距今天数（按截止时间计）。 */
export function daysUntil(dueAt: string): number {
  const ms = new Date(dueAt).getTime() - Date.now();
  return Math.ceil(ms / (24 * 60 * 60 * 1000));
}

/** 统计卡：整卡可点击跳转到对应页面。 */
export function StatCard({
  label,
  value,
  to,
  alert = false,
}: {
  label: string;
  value: number;
  to: string;
  alert?: boolean;
}) {
  return (
    <Link to={to}>
      <Card className="h-full transition-colors hover:bg-accent/50">
        <CardHeader className="p-4 pb-2">
          <CardDescription>{label}</CardDescription>
          <CardTitle
            className={cn("text-2xl", alert && value > 0 && "text-destructive")}
          >
            {value}
          </CardTitle>
        </CardHeader>
      </Card>
    </Link>
  );
}
