import { useState } from "react";
import { Download } from "lucide-react";
import { Button } from "@/components/ui/button";
import type { Deliverable } from "../../types";
import {
  downloadStoredFile,
  formatFileSize,
  shortSha,
} from "./constants";

interface Props {
  deliverable: Deliverable;
}

/**
 * 交付物内容渲染（13.2 节）：git 链接 / 文本 / 文件条目（文件名 + 大小 +
 * sha256 截断 + 下载按钮）。下载走鉴权 blob 流程，403 时提示无权限。
 */
export function DeliverableBody({ deliverable }: Props) {
  const [downloading, setDownloading] = useState(false);

  if (deliverable.type === "git_link") {
    return (
      <a
        href={deliverable.content ?? "#"}
        target="_blank"
        rel="noreferrer"
        className="break-all text-sm text-primary hover:underline"
      >
        {deliverable.content}
      </a>
    );
  }

  if (deliverable.type === "text") {
    return (
      <p className="whitespace-pre-wrap rounded-md bg-muted px-3 py-2 text-sm">
        {deliverable.content}
      </p>
    );
  }

  // file 类型
  const file = deliverable.file;
  if (!file) {
    return (
      <p className="text-sm text-muted-foreground">（文件记录不可用）</p>
    );
  }
  return (
    <div className="flex items-center justify-between gap-2 rounded-md bg-muted px-3 py-2 text-sm">
      <div className="min-w-0">
        <p className="truncate font-medium">{file.original_filename}</p>
        <p className="text-xs text-muted-foreground">
          {formatFileSize(file.size_bytes)} · SHA-256{" "}
          <span className="font-mono" title={file.sha256}>
            {shortSha(file.sha256)}
          </span>
        </p>
      </div>
      <Button
        type="button"
        variant="outline"
        size="sm"
        disabled={downloading}
        onClick={() => {
          setDownloading(true);
          void downloadStoredFile(file.id, file.original_filename).finally(
            () => setDownloading(false),
          );
        }}
      >
        <Download className="size-4" />
        {downloading ? "下载中…" : "下载"}
      </Button>
    </div>
  );
}
