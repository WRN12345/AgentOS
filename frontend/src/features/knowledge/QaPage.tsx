import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation } from "@tanstack/react-query";
import { SendHorizonal } from "lucide-react";
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
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { api, errorMessage } from "../../services/api";
import type { QaResponse, QaSource } from "../../types";

const SOURCE_TYPE_LABELS: Record<string, string> = {
  document: "项目文档",
  history: "历史记录",
  core_memory: "核心记忆",
};

/** 依据原文弹窗（M7.5，设计文档第 11 节）：片段原文 + 按来源类型的追溯入口。 */
function SourceDialog({
  source,
  onClose,
}: {
  source: QaSource | null;
  onClose: () => void;
}) {
  return (
    <Dialog open={source !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{source?.title}</DialogTitle>
          <DialogDescription>
            {SOURCE_TYPE_LABELS[source?.source_type ?? ""] ?? source?.source_type}
            · 答案依据的原文片段
          </DialogDescription>
        </DialogHeader>
        <p className="whitespace-pre-wrap rounded-md bg-muted px-3 py-2 text-sm">
          {source?.snippet}
        </p>
        {source?.source_type === "document" && (
          <Button
            variant="outline"
            size="sm"
            onClick={() => api.downloadFile(`/files/${source.source_id}/download`)}
          >
            下载原文
          </Button>
        )}
        {source?.source_type === "history" && (
          <Link to={`/work-items/${source.source_id}`}>
            <Button variant="outline" size="sm">
              查看关联工作项
            </Button>
          </Link>
        )}
        {source?.source_type === "core_memory" && (
          <Link to="/core-memory">
            <Button variant="outline" size="sm">
              查看核心记忆
            </Button>
          </Link>
        )}
      </DialogContent>
    </Dialog>
  );
}

/** 依据列表（M7.5）：答案下方列出全部依据，点击查看原文。 */
function SourcesList({
  sources,
  onOpen,
}: {
  sources: QaSource[];
  onOpen: (source: QaSource) => void;
}) {
  return (
    <div className="space-y-2">
      <h4 className="text-sm font-medium text-muted-foreground">依据（点击查看原文）</h4>
      <ul className="space-y-2">
        {sources.map((s, i) => (
          <li key={`${s.source_type}-${s.source_id}-${i}`}>
            <button
              type="button"
              className="w-full rounded-md bg-muted px-3 py-2 text-left text-sm hover:bg-muted/70"
              onClick={() => onOpen(s)}
            >
              <span className="mr-2 text-xs text-muted-foreground">[{i + 1}]</span>
              <span className="font-medium">{s.title}</span>
              <span className="block text-muted-foreground line-clamp-2">
                {s.snippet}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

/**
 * 知识库问答页（M7.4，设计文档第 11 节②）：
 * 聊天式单轮提问（本期无多轮、无流式）；命中展示答案与依据，
 * 低于阈值明确拒答并列出最接近的线索（16.13 宁拒答不编造）。
 */
export default function QaPage() {
  const [question, setQuestion] = useState("");
  const [result, setResult] = useState<QaResponse | null>(null);
  const [activeSource, setActiveSource] = useState<QaSource | null>(null);

  const ask = useMutation({
    mutationFn: (q: string) =>
      api.post<QaResponse>("/memory/qa", { question: q }),
    onSuccess: (data) => setResult(data),
    onError: (error) => toast.error(errorMessage(error, "提问失败，请稍后重试")),
  });

  const submit = () => {
    const q = question.trim();
    if (!q || ask.isPending) return;
    ask.mutate(q);
  };

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold">知识库问答</h1>
        <p className="text-sm text-muted-foreground">
          就项目文档、核心记忆与历史记录提问；答案附依据可溯源，查不到会明说
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">提问</CardTitle>
          <CardDescription>例如：我们的部署流程是什么？</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <Textarea
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            placeholder="输入你的问题…"
            rows={3}
            aria-label="问题"
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submit();
            }}
          />
          <Button disabled={!question.trim() || ask.isPending} onClick={submit}>
            <SendHorizonal className="size-4" />
            {ask.isPending ? "检索与生成中…" : "提问"}
          </Button>
        </CardContent>
      </Card>

      {ask.isPending && (
        <Card>
          <CardContent className="space-y-2 pt-6">
            <Skeleton className="h-4 w-3/4" />
            <Skeleton className="h-4 w-1/2" />
          </CardContent>
        </Card>
      )}

      {result && !ask.isPending && (
        <Card>
          <CardHeader>
            <div className="flex items-center gap-2">
              <CardTitle className="text-base">回答</CardTitle>
              {result.status === "answered" ? (
                <Badge variant="default">依据 {result.sources.length} 条</Badge>
              ) : (
                <Badge variant="outline">未找到相关内容</Badge>
              )}
            </div>
          </CardHeader>
          <CardContent className="space-y-3">
            {result.status === "answered" ? (
              <>
                <p className="whitespace-pre-wrap">{result.answer}</p>
                {result.sources.length > 0 && (
                  <SourcesList sources={result.sources} onOpen={setActiveSource} />
                )}
              </>
            ) : (
              <div className="space-y-2">
                <p className="text-sm">
                  知识库里没有找到相关内容——为避免给出不可靠的回答，本次不生成答案。
                </p>
                {result.clues.length > 0 && (
                  <p className="text-sm text-muted-foreground">
                    以下是检索到的最接近的内容，供你人工判断：
                  </p>
                )}
              </div>
            )}
            {result.status === "refused" && result.clues.length > 0 && (
              <ul className="space-y-2">
                {result.clues.map((c) => (
                  <li key={`${c.source_type}-${c.source_id}`}>
                    <button
                      type="button"
                      className="w-full rounded-md bg-muted px-3 py-2 text-left text-sm hover:bg-muted/70"
                      onClick={() => setActiveSource(c)}
                    >
                      <span className="font-medium">{c.title}</span>
                      <span className="block text-muted-foreground">{c.snippet}</span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
      )}

      <SourceDialog source={activeSource} onClose={() => setActiveSource(null)} />
    </div>
  );
}
