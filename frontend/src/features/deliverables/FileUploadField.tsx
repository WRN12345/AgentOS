import { useRef, useState } from "react";
import { FileIcon, RotateCcw, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Progress } from "@/components/ui/progress";
import { api, errorMessage, newIdempotencyKey } from "../../services/api";
import type { StoredFile } from "../../types";
import {
  FILE_ALLOWED_EXTENSIONS,
  formatFileSize,
  validateUploadFile,
} from "./constants";

interface Props {
  /** 关联工作项（上传即写入 stored_files.work_item_id）。 */
  workItemId: string;
  /** 上传成功后回传文件记录（供提交交付物时引用 file_id）。 */
  onUploaded: (file: StoredFile) => void;
  /** 重新选择/清除时回调。 */
  onClear: () => void;
}

type UploadState =
  | { phase: "idle" }
  | { phase: "uploading"; percent: number; name: string }
  | { phase: "done"; file: StoredFile }
  | { phase: "error"; message: string; file: File };

/**
 * 文件上传控件（13.2 节）：选择即上传，XHR onprogress 进度条，
 * 大小/扩展名前置校验（与后端白名单一致），失败后可重试。
 */
export function FileUploadField({ workItemId, onUploaded, onClear }: Props) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [state, setState] = useState<UploadState>({ phase: "idle" });
  const [validationError, setValidationError] = useState<string | null>(null);

  const startUpload = (file: File) => {
    setState({ phase: "uploading", percent: 0, name: file.name });
    const formData = new FormData();
    formData.append("file", file);
    formData.append("work_item_id", workItemId);
    api
      .upload<StoredFile>(
        "/files",
        formData,
        (percent) =>
          setState((s) =>
            s.phase === "uploading" ? { ...s, percent } : s,
          ),
        newIdempotencyKey(),
      )
      .then((stored) => {
        setState({ phase: "done", file: stored });
        onUploaded(stored);
      })
      .catch((error) => {
        setState({
          phase: "error",
          message: errorMessage(error, "上传失败，请重试"),
          file,
        });
      });
  };

  const handleSelect = (file: File | undefined) => {
    if (!file) return;
    setValidationError(null);
    onClear();
    const error = validateUploadFile(file);
    if (error) {
      setValidationError(error);
      setState({ phase: "idle" });
      return;
    }
    startUpload(file);
  };

  const reset = () => {
    setState({ phase: "idle" });
    setValidationError(null);
    onClear();
    if (inputRef.current) {
      inputRef.current.value = "";
    }
  };

  return (
    <div className="space-y-2">
      <Input
        ref={inputRef}
        type="file"
        accept={FILE_ALLOWED_EXTENSIONS.join(",")}
        disabled={state.phase === "uploading"}
        onChange={(e) => handleSelect(e.target.files?.[0])}
      />
      <p className="text-xs text-muted-foreground">
        不超过 20MB，允许类型：{FILE_ALLOWED_EXTENSIONS.join(" ")}
      </p>
      {validationError && (
        <p className="text-sm text-destructive">{validationError}</p>
      )}

      {state.phase === "uploading" && (
        <div className="space-y-1">
          <div className="flex justify-between text-xs text-muted-foreground">
            <span className="truncate">{state.name}</span>
            <span>{state.percent}%</span>
          </div>
          <Progress value={state.percent} />
        </div>
      )}

      {state.phase === "done" && (
        <div className="flex items-center justify-between rounded-md bg-muted px-3 py-2 text-sm">
          <span className="flex min-w-0 items-center gap-2">
            <FileIcon className="size-4 shrink-0" />
            <span className="truncate">
              {state.file.original_filename}
            </span>
            <span className="shrink-0 text-muted-foreground">
              {formatFileSize(state.file.size_bytes)} · 已上传
            </span>
          </span>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={reset}
            aria-label="重新选择"
          >
            <X className="size-4" />
          </Button>
        </div>
      )}

      {state.phase === "error" && (
        <div className="flex items-center justify-between rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">
          <span>{state.message}</span>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => startUpload(state.file)}
          >
            <RotateCcw className="size-4" />
            重试
          </Button>
        </div>
      )}
    </div>
  );
}
