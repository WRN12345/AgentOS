import { toast } from "sonner";
import { api, ApiError, errorMessage } from "../../services/api";
import type { DeliverableType, ReviewDecision } from "../../types";

/** 交付物类型中文文案（7.5 节三类）。 */
export const DELIVERABLE_TYPE_META: Record<
  DeliverableType,
  { label: string; className: string }
> = {
  git_link: { label: "Git 链接", className: "bg-blue-100 text-blue-700" },
  text: { label: "文本", className: "bg-muted text-muted-foreground" },
  file: { label: "文件", className: "bg-green-100 text-green-700" },
};

/** 审核结论文案（7.5 节三种结论）。 */
export const REVIEW_DECISION_META: Record<
  ReviewDecision,
  { label: string; className: string }
> = {
  approve: { label: "通过", className: "bg-green-100 text-green-700" },
  request_changes: {
    label: "要求修改",
    className: "bg-amber-100 text-amber-700",
  },
  reject: { label: "拒绝", className: "bg-red-100 text-red-700" },
};

/* ---------- 文件上传前置校验（与后端白名单一致，14 章） ---------- */

/** 上传大小上限：20MB（与后端配置一致）。 */
export const FILE_MAX_BYTES = 20 * 1024 * 1024;

/** 允许的扩展名白名单（与后端配置一致）。 */
export const FILE_ALLOWED_EXTENSIONS = [
  ".txt",
  ".md",
  ".csv",
  ".json",
  ".pdf",
  ".png",
  ".jpg",
  ".jpeg",
  ".zip",
];

/** 前置校验：返回错误文案，null 表示通过。 */
export function validateUploadFile(file: File): string | null {
  if (file.size > FILE_MAX_BYTES) {
    return `文件超过大小上限 ${formatFileSize(FILE_MAX_BYTES)}`;
  }
  const dot = file.name.lastIndexOf(".");
  const ext = dot >= 0 ? file.name.slice(dot).toLowerCase() : "";
  if (!FILE_ALLOWED_EXTENSIONS.includes(ext)) {
    return `不支持的文件类型 ${ext || "（无扩展名）"}，允许：${FILE_ALLOWED_EXTENSIONS.join(" ")}`;
  }
  return null;
}

/** 人类可读文件大小。 */
export function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** SHA-256 截断显示（前 12 位，全量值放 title 悬浮查看）。 */
export function shortSha(sha256: string): string {
  return sha256.length > 12 ? `${sha256.slice(0, 12)}…` : sha256;
}

/**
 * 触发浏览器下载：fetch blob → ObjectURL → a[download]。
 * 403（无关成员）时 toast 明确提示无权限（16 节）。
 */
export async function downloadStoredFile(
  fileId: string,
  fallbackName: string,
): Promise<void> {
  try {
    const { blob, filename } = await api.downloadFile(
      `/files/${fileId}/download`,
    );
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename || fallbackName;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(url);
  } catch (error) {
    if (error instanceof ApiError && error.status === 403) {
      toast.error("无权限下载该文件");
      return;
    }
    toast.error(errorMessage(error, "下载失败"));
  }
}
