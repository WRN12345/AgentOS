import { useState } from "react";
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
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { api, errorMessage } from "../../services/api";
import type { QaResponse } from "../../types";

/**
 * 知识库问答页（M7.4，设计文档第 11 节②）：
 * 聊天式单轮提问（本期无多轮、无流式）；命中展示答案与依据，
 * 低于阈值明确拒答并列出最接近的线索（16.13 宁拒答不编造）。
 */
export default function QaPage() {
  const [question, setQuestion] = useState("");
  const [result, setResult] = useState<QaResponse | null>(null);

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
              <p className="whitespace-pre-wrap">{result.answer}</p>
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
                  <li
                    key={`${c.source_type}-${c.source_id}`}
                    className="rounded-md bg-muted px-3 py-2 text-sm"
                  >
                    <span className="font-medium">{c.title}</span>
                    <span className="block text-muted-foreground">{c.snippet}</span>
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
      )}
    </div>
  );
}
