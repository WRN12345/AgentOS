import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
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
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
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
import type { StoredFile } from "../../types";
import { formatDateTime } from "../work-items/constants";
import { INDEX_STATUS_META } from "./constants";
import { queryKeys } from "../../lib/queryKeys";

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function StatusBadge({ status }: { status: StoredFile["index_status"] }) {
  const meta = INDEX_STATUS_META[status] ?? { label: status, variant: "outline" as const };
  return <Badge variant={meta.variant}>{meta.label}</Badge>;
}

/** 版本历史弹窗（设计文档第 3 节）：同名文档全部版本，新→旧，旧版本可下载追溯。 */
function VersionsDialog({
  file,
  onClose,
}: {
  file: StoredFile | null;
  onClose: () => void;
}) {
  const { data: versions, isLoading } = useQuery({
    queryKey: queryKeys.files("versions", file?.id),
    queryFn: () => api.get<StoredFile[]>(`/files/${file!.id}/versions`),
    enabled: file !== null,
  });

  return (
    <Dialog open={file !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>版本历史：{file?.original_filename}</DialogTitle>
          <DialogDescription>
            检索与问答只命中最新版本；旧版本仅供人工追溯
          </DialogDescription>
        </DialogHeader>
        {isLoading ? (
          <Skeleton className="h-24 w-full" />
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>版本</TableHead>
                <TableHead>上传时间</TableHead>
                <TableHead>大小</TableHead>
                <TableHead>状态</TableHead>
                <TableHead />
              </TableRow>
            </TableHeader>
            <TableBody>
              {(versions ?? []).map((row) => (
                <TableRow key={row.id}>
                  <TableCell>v{row.version}</TableCell>
                  <TableCell>{formatDateTime(row.created_at)}</TableCell>
                  <TableCell>{formatSize(row.size_bytes)}</TableCell>
                  <TableCell>
                    {row.superseded_by ? (
                      <span className="text-muted-foreground">旧版本</span>
                    ) : (
                      <Badge variant="default">当前版本</Badge>
                    )}
                  </TableCell>
                  <TableCell>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => api.downloadFile(`/files/${row.id}/download`)}
                    >
                      下载
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </DialogContent>
    </Dialog>
  );
}

/**
 * 知识库文档页（M2.11/M2.12）：项目内文件列表 + 索引状态 + 失败重试 + 版本历史。
 * 上传沿用现有入口（工作项/交付物），本页只管"看得见的索引进度"（设计文档第 6 节）。
 */
export default function DocumentsPage() {
  const queryClient = useQueryClient();
  const [versionsOf, setVersionsOf] = useState<StoredFile | null>(null);

  const { data: files, isLoading } = useQuery({
    queryKey: queryKeys.files("current"),
    queryFn: () => api.get<StoredFile[]>("/files"),
  });

  const retryMutation = useMutation({
    mutationFn: (fileId: string) =>
      api.post<StoredFile>(`/files/${fileId}/index-retry`),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.files() });
    },
  });

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold">知识库文档</h1>
        <p className="text-sm text-muted-foreground">
          上传的文档自动进入索引，状态实时可见；检索与问答只命中最新版本
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>文档列表</CardTitle>
          <CardDescription>同名重新上传生成新版本，旧版本保留可查</CardDescription>
        </CardHeader>
        <CardContent>
          {isLoading ? (
            <div className="space-y-2">
              <Skeleton className="h-10 w-full" />
              <Skeleton className="h-10 w-full" />
            </div>
          ) : !files || files.length === 0 ? (
            <p className="text-sm text-muted-foreground">暂无文档</p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>文件名</TableHead>
                  <TableHead>版本</TableHead>
                  <TableHead>大小</TableHead>
                  <TableHead>上传时间</TableHead>
                  <TableHead>索引状态</TableHead>
                  <TableHead>操作</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {files.map((file) => (
                  <TableRow key={file.id}>
                    <TableCell className="font-medium">
                      {file.original_filename}
                    </TableCell>
                    <TableCell>v{file.version}</TableCell>
                    <TableCell>{formatSize(file.size_bytes)}</TableCell>
                    <TableCell>{formatDateTime(file.created_at)}</TableCell>
                    <TableCell>
                      <StatusBadge status={file.index_status} />
                    </TableCell>
                    <TableCell className="space-x-2">
                      {file.index_status === "failed" && (
                        <Button
                          variant="outline"
                          size="sm"
                          disabled={retryMutation.isPending}
                          onClick={() => retryMutation.mutate(file.id)}
                        >
                          重试索引
                        </Button>
                      )}
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => setVersionsOf(file)}
                      >
                        版本历史
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      <VersionsDialog file={versionsOf} onClose={() => setVersionsOf(null)} />
    </div>
  );
}
