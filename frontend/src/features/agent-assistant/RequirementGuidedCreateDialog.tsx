import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { api, errorMessage, newIdempotencyKey } from "../../services/api";
import type {
  AgentRun,
  AgentSuggestion,
  Member,
  WorkItem,
  WorkItemPriority,
} from "../../types";
import { SuggestionContent } from "./SuggestionContent";

type Step = "input" | "waiting" | "confirm";

interface RequirementGuidedCreateDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  members: Member[];
  /** 16 节：外部模型服务时在对话框内同步提示。 */
  llmIsExternal: boolean;
}

/**
 * 「自然语言需求 → Agent 建议 → 人工确认」创建工作项引导（13.1 节，T5.7）。
 *
 * 链路：输入自然语言需求 → POST /agent-analysis（项目级，
 * agent_type=requirement_analyst，仅负责人）→ 轮询运行状态直至成功 →
 * 展示建议并允许人工修改确认参数 → 由前端携带确认后的参数调既有
 * POST /work-items 创建正式工作项；忽略建议则只记录 ignored 反馈，
 * 不产生任何业务写入（原则 2：Agent 不直接写业务表）。
 */
export function RequirementGuidedCreateDialog({
  open,
  onOpenChange,
  members,
  llmIsExternal,
}: RequirementGuidedCreateDialogProps) {
  const queryClient = useQueryClient();
  const [step, setStep] = useState<Step>("input");
  const [requirement, setRequirement] = useState("");
  const [runId, setRunId] = useState<string | null>(null);
  const [suggestion, setSuggestion] = useState<AgentSuggestion | null>(null);

  // 人工确认表单（默认值在拿到建议后预填，可修改）
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [acceptanceCriteria, setAcceptanceCriteria] = useState("");
  const [priority, setPriority] = useState<WorkItemPriority>("medium");
  const [assigneeId, setAssigneeId] = useState("");
  const [dueAt, setDueAt] = useState("");

  const reset = () => {
    setStep("input");
    setRequirement("");
    setRunId(null);
    setSuggestion(null);
    setTitle("");
    setDescription("");
    setAcceptanceCriteria("");
    setPriority("medium");
    setAssigneeId("");
    setDueAt("");
  };

  const close = (next: boolean) => {
    if (!next) reset();
    onOpenChange(next);
  };

  // 第一步：触发 requirement_analyst（项目级入口，leader 限定由后端强校验）
  const trigger = useMutation({
    mutationFn: (prompt: string) =>
      api.post<AgentRun>(
        "/agent-analysis",
        { agent_type: "requirement_analyst", prompt },
        newIdempotencyKey(),
      ),
    onSuccess: (run) => {
      setRunId(run.id);
      setStep("waiting");
    },
    onError: (error) => toast.error(errorMessage(error, "触发 Agent 分析失败")),
  });

  // 第二步：轮询运行状态直至终态（SSE 失效缓存兜底，轮询保证本对话框及时推进）
  const { data: run } = useQuery({
    queryKey: ["agent-runs", "detail", runId],
    queryFn: () => api.get<AgentRun>(`/agent-runs/${runId}`),
    enabled: step === "waiting" && runId !== null,
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status === "succeeded" || status === "failed" ? false : 2000;
    },
  });

  // 运行成功后取该 run 的建议
  const { data: suggestions } = useQuery({
    queryKey: ["agent-suggestions", "by-run", runId],
    queryFn: () => api.get<AgentSuggestion[]>("/agent-suggestions?limit=50"),
    enabled: step === "waiting" && run?.status === "succeeded",
  });

  useEffect(() => {
    if (step !== "waiting" || !suggestions) return;
    const found = suggestions.find((s) => s.run_id === runId);
    if (!found) return;
    setSuggestion(found);
    // 预填确认表单：描述拼接目标/约束/交付物，验收标准逐行
    const content = found.content;
    const asLines = (value: unknown) =>
      Array.isArray(value) ? value.map((v) => String(v)).join("\n") : "";
    const sections = [
      asLines(content.goals) && `【目标】\n${asLines(content.goals)}`,
      asLines(content.constraints) && `【约束】\n${asLines(content.constraints)}`,
      asLines(content.deliverables) &&
        `【交付物】\n${asLines(content.deliverables)}`,
    ].filter(Boolean);
    setDescription(sections.join("\n\n"));
    setAcceptanceCriteria(asLines(content.acceptance_criteria));
    setStep("confirm");
  }, [step, suggestions, runId]);

  // 反馈（best-effort）：采纳/忽略只写 agent_suggestions，不产生业务写入
  const sendFeedback = async (action: "accepted" | "ignored") => {
    if (!suggestion) return;
    try {
      await api.post(
        `/agent-suggestions/${suggestion.id}/feedback`,
        { action },
        newIdempotencyKey(),
      );
      queryClient.invalidateQueries({ queryKey: ["agent-suggestions"] });
    } catch {
      // 反馈失败不阻塞主流程（如重复反馈 409）
    }
  };

  // 第三步：人工确认后由前端调既有 POST /work-items（Agent 不写业务表）
  const createWorkItem = useMutation({
    mutationFn: () =>
      api.post<WorkItem>(
        "/work-items",
        {
          title,
          description: description || null,
          acceptance_criteria: acceptanceCriteria || null,
          priority,
          assignee_id: assigneeId,
          collaborator_ids: [],
          due_at: dueAt ? new Date(`${dueAt}T00:00:00`).toISOString() : null,
        },
        newIdempotencyKey(),
      ),
    onSuccess: async () => {
      toast.success("工作项已创建");
      queryClient.invalidateQueries({ queryKey: ["work-items"] });
      await sendFeedback("accepted");
      close(false);
    },
    onError: (error) => toast.error(errorMessage(error, "创建工作项失败")),
  });

  const ignore = async () => {
    await sendFeedback("ignored");
    toast.success("已忽略该建议，未创建任何工作项");
    close(false);
  };

  // 管理员不参与工作协作：不出现在主执行人候选中
  const activeMembers = members.filter((m) => m.is_active && m.role !== "admin");

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent className="max-h-[90vh] max-w-xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>需求引导创建工作项</DialogTitle>
          <DialogDescription>
            自然语言需求 → Agent 建议 → 人工确认。Agent 只产出建议，确认后才创建正式工作项。
          </DialogDescription>
        </DialogHeader>

        {llmIsExternal && (
          <p className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-800">
            当前使用云端模型服务，输入的需求内容将发送至外部服务，请勿包含敏感信息。
          </p>
        )}

        {step === "input" && (
          <div className="space-y-4">
            <div className="space-y-1">
              <Label>自然语言需求</Label>
              <Textarea
                rows={6}
                placeholder="例如：为 RAG 系统增加检索评估模块，两周内完成，输出评估报告……"
                value={requirement}
                onChange={(e) => setRequirement(e.target.value)}
              />
            </div>
            <DialogFooter>
              <Button
                disabled={!requirement.trim() || trigger.isPending}
                onClick={() => trigger.mutate(requirement.trim())}
              >
                {trigger.isPending ? "触发中…" : "生成 Agent 建议"}
              </Button>
            </DialogFooter>
          </div>
        )}

        {step === "waiting" && (
          <div className="space-y-3 text-sm">
            {run?.status === "failed" ? (
              <>
                <p className="rounded-md bg-red-50 px-3 py-2 text-red-700">
                  Agent 分析失败：{run.error ?? "未知错误"}
                </p>
                <DialogFooter>
                  <Button variant="outline" onClick={() => setStep("input")}>
                    返回修改需求
                  </Button>
                </DialogFooter>
              </>
            ) : (
              <p className="text-muted-foreground">
                Agent 正在分析，请稍候…（通常需要几秒到几十秒）
              </p>
            )}
          </div>
        )}

        {step === "confirm" && suggestion && (
          <div className="space-y-4 text-sm">
            <div className="space-y-2 rounded-md border p-3">
              <h3 className="font-medium">Agent 建议</h3>
              {suggestion.content.summary && <p>{suggestion.content.summary}</p>}
              {suggestion.content.rationale && (
                <p className="text-muted-foreground">
                  理由：{suggestion.content.rationale}
                </p>
              )}
              <SuggestionContent
                suggestionType={suggestion.suggestion_type}
                content={suggestion.content}
              />
              {suggestion.risks && (
                <p className="rounded-md bg-amber-50 px-3 py-2 text-amber-800">
                  {suggestion.risks}
                </p>
              )}
            </div>

            <div className="space-y-3">
              <div className="space-y-1">
                <Label>标题</Label>
                <Input
                  value={title}
                  onChange={(e) => setTitle(e.target.value)}
                  placeholder="请输入工作项标题"
                />
              </div>
              <div className="space-y-1">
                <Label>说明（可编辑）</Label>
                <Textarea
                  rows={5}
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                />
              </div>
              <div className="space-y-1">
                <Label>验收标准（可编辑）</Label>
                <Textarea
                  rows={3}
                  value={acceptanceCriteria}
                  onChange={(e) => setAcceptanceCriteria(e.target.value)}
                />
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div className="space-y-1">
                  <Label>优先级</Label>
                  <Select
                    value={priority}
                    onValueChange={(v) => setPriority(v as WorkItemPriority)}
                  >
                    <SelectTrigger>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="low">低</SelectItem>
                      <SelectItem value="medium">中</SelectItem>
                      <SelectItem value="high">高</SelectItem>
                      <SelectItem value="urgent">紧急</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
                <div className="space-y-1">
                  <Label>截止时间（可选）</Label>
                  <Input
                    type="date"
                    value={dueAt}
                    onChange={(e) => setDueAt(e.target.value)}
                  />
                </div>
              </div>
              <div className="space-y-1">
                <Label>主执行人</Label>
                <Select value={assigneeId} onValueChange={setAssigneeId}>
                  <SelectTrigger>
                    <SelectValue placeholder="选择成员" />
                  </SelectTrigger>
                  <SelectContent>
                    {activeMembers.map((m) => (
                      <SelectItem key={m.id} value={m.id}>
                        {m.display_name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            </div>

            <DialogFooter className="gap-2">
              <Button variant="outline" onClick={ignore}>
                忽略建议
              </Button>
              <Button
                disabled={
                  !title.trim() || !assigneeId || createWorkItem.isPending
                }
                onClick={() => createWorkItem.mutate()}
              >
                {createWorkItem.isPending ? "创建中…" : "确认创建工作项"}
              </Button>
            </DialogFooter>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
