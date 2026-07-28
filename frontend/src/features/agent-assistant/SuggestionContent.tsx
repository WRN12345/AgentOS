import { Badge } from "@/components/ui/badge";

/** 各 suggestion_type 的结构化 content 渲染（10.1 节六个 Agent 的输出结构，见 backend/app/agents/prompts）。 */

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
