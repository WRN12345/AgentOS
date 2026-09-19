import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { queryKeys } from "../../lib/queryKeys";
import { api } from "../../services/api";
import type { Handoff } from "../../types";

export function HandoffInbox() {
  const { data, isLoading, isError } = useQuery({
    queryKey: queryKeys.handoffs("received", "pending"),
    queryFn: () => api.get<Handoff[]>("/handoffs?role=received&status=pending"),
  });
  return (
    <Card>
      <CardHeader><CardTitle>待我接收{data ? `（${data.length}）` : ""}</CardTitle></CardHeader>
      <CardContent className="max-h-80 space-y-3 overflow-y-auto">
        {isLoading && <p className="text-sm text-muted-foreground">正在加载移交…</p>}
        {isError && <p role="alert">待接收移交加载失败，请刷新后重试</p>}
        {data?.length === 0 && <p className="text-sm text-muted-foreground">暂无待接收移交</p>}
        {data?.map((handoff) => (
          <div key={handoff.id} className="space-y-1 rounded-md border p-3 text-sm">
            <p>{handoff.sender.display_name} · {handoff.source_work_item.title}</p>
            <p className="text-muted-foreground">交付物 v{handoff.deliverable.version}</p>
            <Link className="font-medium text-primary hover:underline" to={`/work-items/${handoff.target_work_item.id}#handoff-section`}>
              接收至：{handoff.target_work_item.title}
            </Link>
          </div>
        ))}
      </CardContent>
    </Card>
  );
}
