import { Badge } from "@/components/ui/badge";
import type { RequirementPipelineContent } from "../../types";
import { MEMORY_PROPOSAL_ACTION_LABELS } from "./constants";

/** 各 suggestion_type 的结构化 content 渲染（10.1 节六个 Agent + requirement_pipeline 的输出结构，见 backend/app/agents/prompts）。 */

interface ContentProps {
  suggestionType: string;
  content: Record<string, unknown>;
}

/** 字符串数组小节（goals/constraints/deliverables/acceptance_criteria 等）。 */
function StringList({ title, value }: { title: string; value: unknown }) {
  if (!Array.isArray(value) || value.length === 0) {
    return null;
  }
  return (
    <div>
      <h4 className="mb-1 font-medium text-muted-foreground">{title}</h4>
      <ul className="list-disc space-y-0.5 pl-5">
        {value.map((item, i) => (
          <li key={i}>{String(item)}</li>
        ))}
      </ul>
    </div>
  );
}

function RequirementContent({ content }: { content: Record<string, unknown> }) {
  return (
    <>
      <StringList title="目标" value={content.goals} />
      <StringList title="约束" value={content.constraints} />
      <StringList title="交付物" value={content.deliverables} />
      <StringList title="验收标准" value={content.acceptance_criteria} />
    </>
  );
}

interface AssigneeCandidate {
  member_id?: string;
  display_name?: string;
  reason?: string;
}

function AssignmentContent({ content }: { content: Record<string, unknown> }) {
  const recommended = content.recommended_assignee as AssigneeCandidate | null;
  const candidates = (content.candidates ?? []) as AssigneeCandidate[];
  const adjustments = (content.capability_adjustments ?? []) as {
    member_id?: string;
    tag?: string;
    suggested_proficiency?: number;
    reason?: string;
  }[];
  return (
    <>
      {recommended ? (
        <div>
          <h4 className="mb-1 font-medium text-muted-foreground">推荐主执行人</h4>
          <p>
            {recommended.display_name ?? recommended.member_id}
            {recommended.reason && (
              <span className="text-muted-foreground">（{recommended.reason}）</span>
            )}
          </p>
        </div>
      ) : (
        <p className="text-muted-foreground">暂无合适人选</p>
      )}
      {candidates.length > 0 && (
        <div>
          <h4 className="mb-1 font-medium text-muted-foreground">候选人</h4>
          <ul className="list-disc space-y-0.5 pl-5">
            {candidates.map((c, i) => (
              <li key={c.member_id ?? i}>
                {c.display_name ?? c.member_id}
                {c.reason && (
                  <span className="text-muted-foreground">（{c.reason}）</span>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
      {adjustments.length > 0 && (
        <div>
          <h4 className="mb-1 font-medium text-muted-foreground">
            能力修正建议（仅供参考，不会自动生效）
          </h4>
          <ul className="list-disc space-y-0.5 pl-5">
            {adjustments.map((a, i) => (
              <li key={i}>
                {a.tag} → 熟练度 {a.suggested_proficiency ?? "?"}
                {a.reason && (
                  <span className="text-muted-foreground">（{a.reason}）</span>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
    </>
  );
}

function PlanningContent({ content }: { content: Record<string, unknown> }) {
  const breakdown = (content.work_item_breakdown ?? []) as {
    title?: string;
    description?: string;
    suggested_due_date?: string | null;
    collaborator_hint?: string | null;
  }[];
  return (
    <>
      {breakdown.length > 0 && (
        <div>
          <h4 className="mb-1 font-medium text-muted-foreground">工作项拆分建议</h4>
          <ul className="space-y-1">
            {breakdown.map((w, i) => (
              <li key={i} className="rounded-md bg-muted px-3 py-2">
                <span className="font-medium">{w.title}</span>
                {w.description && (
                  <span className="text-muted-foreground">：{w.description}</span>
                )}
                <span className="block text-xs text-muted-foreground">
                  {w.suggested_due_date && `建议截止：${w.suggested_due_date}`}
                  {w.suggested_due_date && w.collaborator_hint && "；"}
                  {w.collaborator_hint && `协作建议：${w.collaborator_hint}`}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
      <StringList title="协作点" value={content.collaboration_points} />
    </>
  );
}

/** requirement_pipeline 组合建议（2026-07-30 设计文档 §4.2）：方面 + 需求要素 + 拆解/分配 + 协作点。 */
function PipelineContent({ content }: { content: Record<string, unknown> }) {
  const pipeline = content as RequirementPipelineContent;
  const aspects = pipeline.involved_aspects ?? [];
  const breakdown = pipeline.work_item_breakdown ?? [];
  const unresolved = pipeline.unresolved_mentions ?? [];
  return (
    <>
      {aspects.length > 0 && (
        <div>
          <h4 className="mb-1 font-medium text-muted-foreground">涉及方面</h4>
          <div className="flex flex-wrap gap-1">
            {aspects.map((aspect) => (
              <Badge key={aspect} variant="outline">
                {aspect}
              </Badge>
            ))}
          </div>
        </div>
      )}
      <StringList title="目标" value={pipeline.goals} />
      <StringList title="约束" value={pipeline.constraints} />
      <StringList title="交付物" value={pipeline.deliverables} />
      <StringList title="验收标准" value={pipeline.acceptance_criteria} />
      {breakdown.length > 0 && (
        <div>
          <h4 className="mb-1 font-medium text-muted-foreground">
            工作项拆解与分配建议
          </h4>
          <ul className="space-y-1">
            {breakdown.map((w, i) => (
              <li key={i} className="rounded-md bg-muted px-3 py-2">
                <span className="font-medium">{w.title}</span>
                {w.priority && (
                  <Badge variant="outline" className="ml-2">
                    {w.priority}
                  </Badge>
                )}
                {w.user_specified && (
                  <Badge className="ml-1 bg-blue-100 text-blue-700">
                    按需求指定
                  </Badge>
                )}
                {w.description && (
                  <span className="block text-muted-foreground">
                    {w.description}
                  </span>
                )}
                <span className="block text-xs text-muted-foreground">
                  {w.suggested_due_at && `建议截止：${w.suggested_due_at}`}
                  {w.recommended_assignee &&
                    `${w.suggested_due_at ? "；" : ""}推荐：${w.recommended_assignee.display_name}`}
                  {w.recommended_assignee?.reason &&
                    `（${w.recommended_assignee.reason}）`}
                </span>
                {(w.candidates ?? []).length > 0 && (
                  <span className="block text-xs text-muted-foreground">
                    候选：
                    {(w.candidates ?? [])
                      .map((c) => c.display_name ?? c.member_id)
                      .join("、")}
                  </span>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
      <StringList title="协作点" value={pipeline.collaboration_points} />
      {unresolved.length > 0 && (
        <p className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-amber-800">
          以下指定人选未能匹配到成员：{unresolved.join("、")}
        </p>
      )}
      <StringList title="风险" value={pipeline.risks} />
    </>
  );
}

/** dev_doc_review 初审结论（verdict）展示。 */
export const DEV_DOC_VERDICT_META: Record<
  string,
  { label: string; className: string }
> = {
  sufficient: { label: "内容完备", className: "bg-green-100 text-green-700" },
  needs_work: { label: "建议补充", className: "bg-amber-100 text-amber-700" },
};

/** dev_doc_review 初审建议（2026-07-30 设计文档 §4.4）：结论 + 完整性检查 + 对齐度 + 风险。 */
function DevDocReviewContent({ content }: { content: Record<string, unknown> }) {
  const checklist = (content.checklist ?? []) as {
    aspect?: string;
    verdict?: string;
    note?: string;
  }[];
  const verdict = typeof content.verdict === "string" ? content.verdict : "";
  const verdictMeta = DEV_DOC_VERDICT_META[verdict];
  return (
    <>
      {verdict && (
        <div>
          <Badge className={verdictMeta?.className ?? ""}>
            {verdictMeta?.label ?? verdict}
          </Badge>
        </div>
      )}
      {checklist.length > 0 && (
        <div>
          <h4 className="mb-1 font-medium text-muted-foreground">完整性检查</h4>
          <ul className="space-y-1">
            {checklist.map((c, i) => (
              <li key={i} className="rounded-md bg-muted px-3 py-2">
                <span className="font-medium">{c.aspect}</span>
                {c.verdict && (
                  <Badge variant="outline" className="ml-2">
                    {c.verdict}
                  </Badge>
                )}
                {c.note && (
                  <span className="block text-muted-foreground">{c.note}</span>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
      {typeof content.alignment === "string" && content.alignment && (
        <div>
          <h4 className="mb-1 font-medium text-muted-foreground">
            与验收标准对齐度
          </h4>
          <p className="whitespace-pre-wrap">{content.alignment}</p>
        </div>
      )}
      <StringList title="风险提示" value={content.risks} />
    </>
  );
}

const RISK_TYPE_LABELS: Record<string, string> = {
  overdue: "逾期/临期",
  blocked: "阻塞",
  frequent_transfer: "频繁转派",
  collaboration_wait: "协作等待",
};

const SEVERITY_META: Record<string, { label: string; className: string }> = {
  high: { label: "高", className: "bg-red-100 text-red-700" },
  medium: { label: "中", className: "bg-amber-100 text-amber-700" },
  low: { label: "低", className: "bg-gray-100 text-gray-700" },
};

function RiskContent({ content }: { content: Record<string, unknown> }) {
  const risks = (content.risks ?? []) as {
    type?: string;
    title?: string;
    severity?: string;
    detail?: string;
  }[];
  if (risks.length === 0) {
    return <p className="text-muted-foreground">本次扫描未发现明显风险</p>;
  }
  return (
    <ul className="space-y-1">
      {risks.map((r, i) => (
        <li key={i} className="rounded-md bg-muted px-3 py-2">
          <span className="mr-2 inline-flex gap-1 align-middle">
            <Badge className={SEVERITY_META[r.severity ?? ""]?.className ?? ""}>
              {SEVERITY_META[r.severity ?? ""]?.label ?? r.severity}
            </Badge>
            <Badge variant="outline">
              {RISK_TYPE_LABELS[r.type ?? ""] ?? r.type}
            </Badge>
          </span>
          <span className="font-medium">{r.title}</span>
          {r.detail && (
            <span className="block text-muted-foreground">{r.detail}</span>
          )}
        </li>
      ))}
    </ul>
  );
}

const VERDICT_META: Record<string, { label: string; className: string }> = {
  pass: { label: "通过", className: "bg-green-100 text-green-700" },
  fail: { label: "不通过", className: "bg-red-100 text-red-700" },
  uncertain: { label: "待人工核实", className: "bg-amber-100 text-amber-700" },
};

function ReviewContent({ content }: { content: Record<string, unknown> }) {
  const checklist = (content.checklist ?? []) as {
    checkpoint?: string;
    verdict?: string;
    evidence?: string;
  }[];
  if (checklist.length === 0) {
    return null;
  }
  return (
    <ul className="space-y-1">
      {checklist.map((c, i) => (
        <li key={i} className="rounded-md bg-muted px-3 py-2">
          <span className="mr-2 inline-block align-middle">
            <Badge className={VERDICT_META[c.verdict ?? ""]?.className ?? ""}>
              {VERDICT_META[c.verdict ?? ""]?.label ?? c.verdict}
            </Badge>
          </span>
          <span className="font-medium">{c.checkpoint}</span>
          {c.evidence && (
            <span className="block text-muted-foreground">依据：{c.evidence}</span>
          )}
        </li>
      ))}
    </ul>
  );
}

function SummaryContent({ content }: { content: Record<string, unknown> }) {
  return (
    <>
      {typeof content.progress === "string" && content.progress && (
        <div>
          <h4 className="mb-1 font-medium text-muted-foreground">项目进展</h4>
          <p className="whitespace-pre-wrap">{content.progress}</p>
        </div>
      )}
      <StringList title="已完成事项" value={content.completed} />
      <StringList title="待审批" value={content.pending_approvals} />
      <StringList title="风险提示" value={content.risks} />
    </>
  );
}

/** 核心记忆提议（M4.4/M4.6，设计文档第 8 节）：动作 + 内容预览 + 目标条目 + 理由。 */
function MemoryProposalContent({ content }: { content: Record<string, unknown> }) {
  const action = typeof content.action === "string" ? content.action : "";
  const actionLabel = MEMORY_PROPOSAL_ACTION_LABELS[action] ?? action;
  const reason = typeof content.reason === "string" ? content.reason : "";
  const text = typeof content.content === "string" ? content.content : "";
  const targetIds = [
    ...(Array.isArray(content.entry_ids) ? content.entry_ids : []),
    ...(typeof content.entry_id === "string" ? [content.entry_id] : []),
  ] as string[];
  return (
    <>
      <div>
        <h4 className="mb-1 font-medium text-muted-foreground">提议动作</h4>
        <Badge variant="outline">{actionLabel}</Badge>
      </div>
      {text && (
        <div>
          <h4 className="mb-1 font-medium text-muted-foreground">
            {action === "deprecate" ? "条目说明" : "条目内容（确认后生效）"}
          </h4>
          <p className="whitespace-pre-wrap rounded-md bg-muted px-3 py-2">
            {text}
          </p>
        </div>
      )}
      {targetIds.length > 0 && (
        <div>
          <h4 className="mb-1 font-medium text-muted-foreground">目标条目</h4>
          <ul className="space-y-0.5 text-xs text-muted-foreground">
            {targetIds.map((id) => (
              <li key={id}>{id}</li>
            ))}
          </ul>
        </div>
      )}
      {reason && (
        <div>
          <h4 className="mb-1 font-medium text-muted-foreground">理由</h4>
          <p className="whitespace-pre-wrap">{reason}</p>
        </div>
      )}
    </>
  );
}

const TYPE_RENDERERS: Record<
  string,
  (props: { content: Record<string, unknown> }) => React.JSX.Element | null
> = {
  requirement: RequirementContent,
  assignment: AssignmentContent,
  planning: PlanningContent,
  risk: RiskContent,
  review: ReviewContent,
  summary: SummaryContent,
  pipeline: PipelineContent,
  dev_doc_review: DevDocReviewContent,
  memory_proposal: MemoryProposalContent,
};

/**
 * 建议内容的结构化渲染：summary/rationale 由调用方统一展示，
 * 本组件渲染各类型自有字段；未识别的类型回退为 JSON 展示。
 */
export function SuggestionContent({ suggestionType, content }: ContentProps) {
  const Renderer = TYPE_RENDERERS[suggestionType];
  if (!Renderer) {
    const extra = Object.fromEntries(
      Object.entries(content).filter(
        ([key]) => key !== "summary" && key !== "rationale",
      ),
    );
    if (Object.keys(extra).length === 0) {
      return null;
    }
    return (
      <pre className="overflow-x-auto rounded-md bg-muted px-3 py-2 text-xs">
        {JSON.stringify(extra, null, 2)}
      </pre>
    );
  }
  return <Renderer content={content} />;
}
