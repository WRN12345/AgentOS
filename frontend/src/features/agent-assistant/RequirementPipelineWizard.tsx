import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CircleCheckIcon,
  Loader2Icon,
  OctagonXIcon,
  Plus,
  Sparkles,
  Trash2,
  TriangleAlertIcon,
} from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
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
  PipelineAssigneeCandidate,
  RequirementPipelineContent,
  WorkItem,
  WorkItemPriority,
} from "../../types";

type Step = "input" | "waiting" | "confirm" | "creating";

/** 确认步骤中可编辑的工作项草稿（由拆解建议预填，可增删改）。 */
interface DraftItem {
  key: string;
  title: string;
  description: string;
  acceptanceCriteria: string;
  priority: WorkItemPriority;
  /** date input 值（yyyy-mm-dd），空串表示无 DDL。 */
  dueAt: string;
  assigneeId: string;
  recommended: PipelineAssigneeCandidate | null;
  userSpecified: boolean;
}

interface CreateResult {
  key: string;
  title: string;
  status: "pending" | "creating" | "success" | "failed";
  error?: string;
}

interface RequirementPipelineWizardProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  members: Member[];
  /** 16 节：外部模型服务时在向导内同步提示。 */
  llmIsExternal: boolean;
  /**
   * 从既有 pipeline 建议恢复（建议中心"采纳并创建工作项"入口）：
   * 跳过输入需求与等待分析，直接用该建议的拆解结果进入确认步骤。
   */
  resumeSuggestion?: AgentSuggestion | null;
}

/** Agent 输出 P0–P3，映射为工作项优先级；兼容直接输出枚举值的情况。 */
function mapPriority(value: unknown): WorkItemPriority {
  const raw = String(value ?? "").trim();
  if (["low", "medium", "high", "urgent"].includes(raw)) {
    return raw as WorkItemPriority;
  }
  const map: Record<string, WorkItemPriority> = {
    P0: "urgent",
    P1: "high",
    P2: "medium",
    P3: "low",
  };
  return map[raw.toUpperCase()] ?? "medium";
}

function normalizeCandidate(value: unknown): PipelineAssigneeCandidate | null {
  if (!value || typeof value !== "object") return null;
  const v = value as Record<string, unknown>;
  if (v.member_id === undefined || v.member_id === null) return null;
  return {
    member_id: String(v.member_id),
    display_name: String(v.display_name ?? v.member_id),
    reason: typeof v.reason === "string" ? v.reason : undefined,
  };
}

/** 把 pipeline 建议的拆解结果映射为可编辑草稿（key 复用幂等键生成器，兼容非安全上下文）。 */
function draftsFromContent(content: RequirementPipelineContent): DraftItem[] {
  return (content.work_item_breakdown ?? []).map((item) => {
    const recommended = normalizeCandidate(item.recommended_assignee);
    return {
      key: newIdempotencyKey(),
      title: item.title ?? "",
      description: item.description ?? "",
      acceptanceCriteria: item.acceptance_criteria ?? "",
      priority: mapPriority(item.priority),
      dueAt: item.suggested_due_at ?? "",
      assigneeId: recommended?.member_id ?? "",
      recommended,
      userSpecified: item.user_specified ?? false,
    };
  });
}

/**
 * 需求拆解流水线向导（2026-07-30 设计文档 §5），取代原 RequirementGuidedCreateDialog。
 *
 * 链路：输入自然语言需求（可指定人选）→ POST /agent-analysis
 * （agent_type=requirement_pipeline，仅负责人）→ 2s 轮询运行状态 →
 * 确认/编辑拆解出的多个工作项 → 前端逐项调既有 POST /work-items 批量创建，
 * 全部成功后写 accepted 反馈；忽略只写 ignored 反馈，不产生业务写入
 * （原则 2：人类决定，Agent 建议）。
 *
 * 两种入口：新建模式（工作项页/建议中心顶部按钮，走完整四步）；
 * 恢复模式（建议中心对 pending 的 pipeline 建议点"采纳并创建工作项"，
 * 传入 resumeSuggestion，跳过输入与等待，直接确认其拆解结果）。
 */
export function RequirementPipelineWizard({
  open,
  onOpenChange,
  members,
  llmIsExternal,
  resumeSuggestion = null,
}: RequirementPipelineWizardProps) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [step, setStep] = useState<Step>("input");
  const [requirement, setRequirement] = useState("");
  const [runId, setRunId] = useState<string | null>(null);
  const [suggestion, setSuggestion] = useState<AgentSuggestion | null>(null);
  const [drafts, setDrafts] = useState<DraftItem[]>([]);
  const [pipelineContent, setPipelineContent] =
    useState<RequirementPipelineContent | null>(null);
  const [results, setResults] = useState<CreateResult[]>([]);
  const [creating, setCreating] = useState(false);

  const reset = () => {
    setStep("input");
    setRequirement("");
    setRunId(null);
    setSuggestion(null);
    setDrafts([]);
    setPipelineContent(null);
    setResults([]);
    setCreating(false);
  };

  const close = (next: boolean) => {
    if (!next) reset();
    onOpenChange(next);
  };

  // 第一步：触发 requirement_pipeline（项目级入口，leader 限定由后端强校验）
  const trigger = useMutation({
    mutationFn: (prompt: string) =>
      api.post<AgentRun>(
        "/agent-analysis",
        { agent_type: "requirement_pipeline", prompt },
        newIdempotencyKey(),
      ),
    onSuccess: (run) => {
      setRunId(run.id);
      setStep("waiting");
    },
    onError: (error) => toast.error(errorMessage(error, "触发 Agent 分析失败")),
  });

  // 第二步：轮询运行状态直至终态（2s 轮询，与 SSE 失效缓存互补）
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
    const content = found.content as RequirementPipelineContent;
    setPipelineContent(content);
    setDrafts(draftsFromContent(content));
    setStep("confirm");
  }, [step, suggestions, runId]);

  // 从既有建议恢复（建议中心"采纳并创建工作项"）：跳过输入/等待，直接进入确认步骤
  useEffect(() => {
    if (!open || !resumeSuggestion) return;
    const content = resumeSuggestion.content as RequirementPipelineContent;
    setSuggestion(resumeSuggestion);
    setPipelineContent(content);
    setDrafts(draftsFromContent(content));
    setStep("confirm");
  }, [open, resumeSuggestion]);

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

  const updateDraft = (key: string, patch: Partial<DraftItem>) => {
    setDrafts((prev) =>
      prev.map((d) => (d.key === key ? { ...d, ...patch } : d)),
    );
  };

  const removeDraft = (key: string) => {
    setDrafts((prev) => prev.filter((d) => d.key !== key));
  };

  const addDraft = () => {
    setDrafts((prev) => [
      ...prev,
      {
        key: newIdempotencyKey(),
        title: "",
        description: "",
        acceptanceCriteria: "",
        priority: "medium",
        dueAt: "",
        assigneeId: "",
        recommended: null,
        userSpecified: false,
      },
    ]);
  };

  // 第四步：逐项调既有 POST /work-items（Agent 不写业务表，自动带幂等键）
  const createItems = async (items: DraftItem[]) => {
    setCreating(true);
    let succeeded = 0;
    const failed: string[] = [];
    for (const item of items) {
      setResults((prev) =>
        prev.map((r) =>
          r.key === item.key ? { ...r, status: "creating", error: undefined } : r,
        ),
      );
      try {
        await api.post<WorkItem>(
          "/work-items",
          {
            title: item.title.trim(),
            description: item.description.trim() || null,
            acceptance_criteria: item.acceptanceCriteria.trim() || null,
            priority: item.priority,
            assignee_id: item.assigneeId,
            collaborator_ids: [],
            due_at: item.dueAt
              ? new Date(`${item.dueAt}T00:00:00`).toISOString()
              : null,
          },
          newIdempotencyKey(),
        );
        succeeded += 1;
        setResults((prev) =>
          prev.map((r) => (r.key === item.key ? { ...r, status: "success" } : r)),
        );
      } catch (error) {
        failed.push(item.key);
        setResults((prev) =>
          prev.map((r) =>
            r.key === item.key
              ? { ...r, status: "failed", error: errorMessage(error, "创建失败") }
              : r,
          ),
        );
      }
    }
    setCreating(false);
    queryClient.invalidateQueries({ queryKey: ["work-items"] });
    if (failed.length === 0) {
      await sendFeedback("accepted");
      toast.success(`已批量创建 ${succeeded} 个工作项`);
      close(false);
      navigate("/work-items");
    } else {
      // 部分失败：不写 accepted 反馈，保留建议供重试
      toast.error(`${failed.length} 个工作项创建失败，可重试失败项`);
    }
  };

  const startCreate = () => {
    setResults(
      drafts.map((d) => ({ key: d.key, title: d.title, status: "pending" })),
    );
    setStep("creating");
    void createItems(drafts);
  };

  const retryFailed = () => {
    const failedKeys = new Set(
      results.filter((r) => r.status === "failed").map((r) => r.key),
    );
    void createItems(drafts.filter((d) => failedKeys.has(d.key)));
  };

  const ignore = async () => {
    await sendFeedback("ignored");
    toast.success("已忽略该建议，未创建任何工作项");
    close(false);
  };

  // 管理员不参与工作协作：不出现在主执行人候选中
  const activeMembers = members.filter((m) => m.is_active && m.role !== "admin");
  const unresolved = pipelineContent?.unresolved_mentions ?? [];
  const draftsValid =
    drafts.length > 0 && drafts.every((d) => d.title.trim() && d.assigneeId);
  const hasFailed = results.some((r) => r.status === "failed");

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent className="max-h-[90vh] max-w-3xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>需求拆解向导</DialogTitle>
          <DialogDescription>
            自然语言需求 → Agent 拆解与分配建议 → 人工确认 →
            批量创建。Agent 只产出建议，确认后才创建正式工作项。
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
                rows={8}
                placeholder="例如：为 RAG 系统增加检索评估模块，两周内完成，输出评估报告……"
                value={requirement}
                onChange={(e) => setRequirement(e.target.value)}
              />
              <p className="text-xs text-muted-foreground">
                可在文中直接指定人选，如：接口部分给张三。Agent 会尊重指定并做合理性校验。
              </p>
            </div>
            <DialogFooter>
              <Button
                disabled={!requirement.trim() || trigger.isPending}
                onClick={() => trigger.mutate(requirement.trim())}
              >
                <Sparkles className="size-4" />
                {trigger.isPending ? "触发中…" : "生成拆解方案"}
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
                <DialogFooter className="gap-2">
                  <Button variant="outline" onClick={() => setStep("input")}>
                    返回修改需求
                  </Button>
                  <Button
                    disabled={trigger.isPending}
                    onClick={() => trigger.mutate(requirement.trim())}
                  >
                    {trigger.isPending ? "重试中…" : "重试"}
                  </Button>
                </DialogFooter>
              </>
            ) : (
              <p className="flex items-center gap-2 text-muted-foreground">
                <Loader2Icon className="size-4 animate-spin" />
                Agent 正在分析并拆解需求，请稍候…（通常需要几秒到几十秒）
              </p>
            )}
          </div>
        )}

        {step === "confirm" && suggestion && pipelineContent && (
          <div className="space-y-4 text-sm">
            {unresolved.length > 0 && (
              <p className="flex items-center gap-2 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-amber-800">
                <TriangleAlertIcon className="size-4 shrink-0" />
                以下指定人选未能匹配到成员，请手动选择主执行人：
                {unresolved.join("、")}
              </p>
            )}

            <div className="space-y-2 rounded-md border p-3">
              <h3 className="font-medium">分析结果</h3>
              {pipelineContent.summary && <p>{pipelineContent.summary}</p>}
              {(pipelineContent.involved_aspects ?? []).length > 0 && (
                <div className="flex flex-wrap items-center gap-1">
                  <span className="text-muted-foreground">涉及方面：</span>
                  {(pipelineContent.involved_aspects ?? []).map((aspect) => (
                    <Badge key={aspect} variant="outline">
                      {aspect}
                    </Badge>
                  ))}
                </div>
              )}
              {(["goals", "constraints", "risks"] as const).map((field) => {
                const values = pipelineContent[field] ?? [];
                if (values.length === 0) return null;
                const labels = { goals: "目标", constraints: "约束", risks: "风险" };
                return (
                  <p key={field} className="text-muted-foreground">
                    {labels[field]}：{values.join("；")}
                  </p>
                );
              })}
              {(pipelineContent.collaboration_points ?? []).length > 0 && (
                <p className="text-muted-foreground">
                  协作点（仅供参考，不自动创建协作请求）：
                  {(pipelineContent.collaboration_points ?? []).join("；")}
                </p>
              )}
            </div>

            <div className="space-y-3">
              {drafts.map((draft, index) => (
                <div key={draft.key} className="space-y-3 rounded-md border p-3">
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-medium">工作项 {index + 1}</span>
                    <div className="flex items-center gap-2">
                      {draft.userSpecified && (
                        <Badge className="bg-blue-100 text-blue-700">
                          按需求指定
                        </Badge>
                      )}
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => removeDraft(draft.key)}
                      >
                        <Trash2 className="size-4" />
                        删除
                      </Button>
                    </div>
                  </div>
                  <div className="space-y-1">
                    <Label>标题</Label>
                    <Input
                      value={draft.title}
                      onChange={(e) =>
                        updateDraft(draft.key, { title: e.target.value })
                      }
                      placeholder="请输入工作项标题"
                    />
                  </div>
                  <div className="space-y-1">
                    <Label>说明</Label>
                    <Textarea
                      rows={3}
                      value={draft.description}
                      onChange={(e) =>
                        updateDraft(draft.key, { description: e.target.value })
                      }
                    />
                  </div>
                  <div className="space-y-1">
                    <Label>验收标准</Label>
                    <Textarea
                      rows={2}
                      value={draft.acceptanceCriteria}
                      onChange={(e) =>
                        updateDraft(draft.key, {
                          acceptanceCriteria: e.target.value,
                        })
                      }
                    />
                  </div>
                  <div className="grid grid-cols-2 gap-3 md:grid-cols-3">
                    <div className="space-y-1">
                      <Label>优先级</Label>
                      <Select
                        value={draft.priority}
                        onValueChange={(v) =>
                          updateDraft(draft.key, {
                            priority: v as WorkItemPriority,
                          })
                        }
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
                        value={draft.dueAt}
                        onChange={(e) =>
                          updateDraft(draft.key, { dueAt: e.target.value })
                        }
                      />
                    </div>
                    <div className="space-y-1">
                      <Label>主执行人</Label>
                      <Select
                        value={draft.assigneeId}
                        onValueChange={(v) =>
                          updateDraft(draft.key, { assigneeId: v })
                        }
                      >
                        <SelectTrigger>
                          <SelectValue placeholder="选择成员" />
                        </SelectTrigger>
                        <SelectContent>
                          {/* Agent 推荐置顶，其余成员按原顺序 */}
                          {[...activeMembers]
                            .sort((a, b) => {
                              const rec = draft.recommended?.member_id;
                              if (a.id === rec) return -1;
                              if (b.id === rec) return 1;
                              return 0;
                            })
                            .map((m) => (
                              <SelectItem key={m.id} value={m.id}>
                                {m.display_name}
                                {m.id === draft.recommended?.member_id &&
                                  "（推荐）"}
                              </SelectItem>
                            ))}
                        </SelectContent>
                      </Select>
                      {draft.recommended?.reason && (
                        <p className="text-xs text-muted-foreground">
                          推荐理由：{draft.recommended.reason}
                        </p>
                      )}
                    </div>
                  </div>
                </div>
              ))}
              <Button variant="outline" size="sm" onClick={addDraft}>
                <Plus className="size-4" />
                新增工作项
              </Button>
            </div>

            <DialogFooter className="gap-2">
              <Button variant="outline" onClick={ignore}>
                忽略建议
              </Button>
              <Button disabled={!draftsValid} onClick={startCreate}>
                确认批量创建（{drafts.length} 项）
              </Button>
            </DialogFooter>
          </div>
        )}

        {step === "creating" && (
          <div className="space-y-3 text-sm">
            <ul className="space-y-1">
              {results.map((r) => (
                <li
                  key={r.key}
                  className="flex items-center gap-2 rounded-md bg-muted px-3 py-2"
                >
                  {r.status === "success" && (
                    <CircleCheckIcon className="size-4 shrink-0 text-green-600" />
                  )}
                  {r.status === "failed" && (
                    <OctagonXIcon className="size-4 shrink-0 text-red-600" />
                  )}
                  {(r.status === "pending" || r.status === "creating") && (
                    <Loader2Icon
                      className={`size-4 shrink-0 text-muted-foreground ${
                        r.status === "creating" ? "animate-spin" : ""
                      }`}
                    />
                  )}
                  <span className="font-medium">{r.title}</span>
                  {r.status === "failed" && (
                    <span className="text-red-700">{r.error}</span>
                  )}
                </li>
              ))}
            </ul>
            {hasFailed && (
              <p className="text-muted-foreground">
                已成功创建的工作项不受影响；失败项可重试，或关闭后在建议中心重新处理该建议。
              </p>
            )}
            <DialogFooter className="gap-2">
              <Button
                variant="outline"
                disabled={creating}
                onClick={() => close(false)}
              >
                关闭
              </Button>
              {hasFailed && (
                <Button disabled={creating} onClick={retryFailed}>
                  {creating ? "重试中…" : "重试失败项"}
                </Button>
              )}
            </DialogFooter>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
