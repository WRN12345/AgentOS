import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { api, errorMessage, newIdempotencyKey } from "../../services/api";
import { requirementStatus, type Requirement } from "./types";

export function RequirementCard({
  requirement,
  adminPath,
  onChanged,
}: {
  requirement: Requirement;
  adminPath?: string;
  onChanged: (updated: Requirement) => void;
}) {
  const [editingVersion, setEditingVersion] = useState<number | null>(null);
  const editing = editingVersion !== null;
  const editOutdated = editing && editingVersion !== requirement.version;
  const [title, setTitle] = useState(requirement.title);
  const [description, setDescription] = useState(requirement.description);
  const [acceptance, setAcceptance] = useState(requirement.acceptance_criteria);
  const [questions, setQuestions] = useState(
    requirement.clarification_questions,
  );
  const [note, setNote] = useState("");
  const editable = ["draft", "confirmed", "clarification_requested"].includes(
    requirement.status,
  );
  const startEditing = () => {
    setTitle(requirement.title);
    setDescription(requirement.description);
    setAcceptance(requirement.acceptance_criteria);
    setQuestions(requirement.clarification_questions);
    setEditingVersion(requirement.version);
  };
  const mutation = useMutation({
    mutationFn: (
      action:
        "save" | "confirm" | "exclude" | "dispatch" | "accept" | "clarify",
    ) => {
      const path = `${adminPath ?? "/project-requirements"}/${requirement.id}`;
      return action === "save"
        ? api.patch<Requirement>(
            path,
            {
              version: editingVersion,
              title,
              description,
              acceptance_criteria: acceptance,
              clarification_questions: questions,
            },
            newIdempotencyKey(),
          )
        : api.post<Requirement>(
            `${path}/${action}`,
            {
              version: requirement.version,
              ...(action === "clarify" ? { note: note.trim() } : {}),
            },
            newIdempotencyKey(),
          );
    },
    onSuccess: (updated) => {
      onChanged(updated);
      setEditingVersion(null);
      setNote("");
    },
  });
  return (
    <Card className="min-w-0">
      <CardHeader className="flex-row flex-wrap items-center justify-between gap-2">
        <CardTitle className="break-words">{requirement.title}</CardTitle>
        <Badge variant="secondary">
          {requirementStatus[requirement.status]}
        </Badge>
      </CardHeader>
      <CardContent className="space-y-4 text-sm">
        {editing ? (
          <fieldset disabled={mutation.isPending} className="space-y-3">
            <label className="block space-y-1">
              <span>标题</span>
              <Input value={title} onChange={(e) => setTitle(e.target.value)} />
            </label>
            <label className="block space-y-1">
              <span>描述</span>
              <Textarea
                value={description}
                onChange={(e) => setDescription(e.target.value)}
              />
            </label>
            <label className="block space-y-1">
              <span>验收标准</span>
              <Textarea
                value={acceptance}
                onChange={(e) => setAcceptance(e.target.value)}
              />
            </label>
            <label className="block space-y-1">
              <span>澄清问题</span>
              <Textarea
                value={questions}
                onChange={(e) => setQuestions(e.target.value)}
              />
            </label>
            <p className="text-xs text-muted-foreground">
              保存后为草稿，需要重新确认。确认前请填写验收标准并清空已解决的澄清问题。
            </p>
            {editOutdated && (
              <p role="alert" className="text-destructive">
                需求已有新版本，当前修改已保留。请核对最新内容后重新编辑。
              </p>
            )}
          </fieldset>
        ) : (
          <dl className="space-y-3">
            <div>
              <dt className="font-medium">描述</dt>
              <dd className="whitespace-pre-wrap break-words">
                {requirement.description || "未填写"}
              </dd>
            </div>
            <div>
              <dt className="font-medium">验收标准</dt>
              <dd className="whitespace-pre-wrap break-words">
                {requirement.acceptance_criteria || "未填写"}
              </dd>
            </div>
            {requirement.clarification_questions && (
              <div>
                <dt className="font-medium">澄清问题</dt>
                <dd className="whitespace-pre-wrap break-words">
                  {requirement.clarification_questions}
                </dd>
              </div>
            )}
          </dl>
        )}
        {adminPath && requirement.sources.length > 0 && (
          <div className="space-y-2">
            <h3 className="font-medium">原始来源摘录</h3>
            {requirement.sources.map((source, i) => (
              <blockquote
                key={`${source.chunk_id}-${i}`}
                className="border-l-2 pl-3"
              >
                <p className="text-xs text-muted-foreground">
                  {source.filename}
                </p>
                <p className="whitespace-pre-wrap break-words">
                  {source.quote}
                </p>
              </blockquote>
            ))}
          </div>
        )}
        {requirement.leader_note && (
          <div>
            <h3 className="font-medium">负责人反馈</h3>
            <p className="whitespace-pre-wrap break-words">
              {requirement.leader_note}
            </p>
          </div>
        )}
        {mutation.isError && (
          <p role="alert" className="text-destructive">
            {errorMessage(mutation.error, "操作失败，请重试或刷新需求后再操作")}
          </p>
        )}
        <div className="flex flex-wrap gap-2">
          {adminPath &&
            (editable || editing) &&
            (editing ? (
              <>
                <Button
                  disabled={
                    mutation.isPending ||
                    !title.trim() ||
                    !description.trim() ||
                    editOutdated ||
                    !editable
                  }
                  onClick={() => mutation.mutate("save")}
                >
                  保存草稿
                </Button>
                <Button
                  variant="outline"
                  disabled={mutation.isPending}
                  onClick={() => setEditingVersion(null)}
                >
                  取消
                </Button>
                {editOutdated && editable && (
                  <Button
                    variant="outline"
                    disabled={mutation.isPending}
                    onClick={startEditing}
                  >
                    放弃修改并加载最新版本
                  </Button>
                )}
              </>
            ) : (
              <>
                <Button
                  variant="outline"
                  disabled={mutation.isPending}
                  onClick={startEditing}
                >
                  编辑需求
                </Button>
                {requirement.status === "draft" && (
                  <Button
                    disabled={
                      mutation.isPending ||
                      !requirement.acceptance_criteria.trim() ||
                      !!requirement.clarification_questions.trim()
                    }
                    onClick={() => mutation.mutate("confirm")}
                  >
                    确认需求
                  </Button>
                )}
                <Button
                  variant="outline"
                  disabled={mutation.isPending}
                  onClick={() => mutation.mutate("exclude")}
                >
                  排除需求
                </Button>
                {requirement.status === "confirmed" && (
                  <Button
                    disabled={mutation.isPending}
                    onClick={() => mutation.mutate("dispatch")}
                  >
                    派发给负责人
                  </Button>
                )}
              </>
            ))}
          {!adminPath && requirement.status === "dispatched" && (
            <Button
              disabled={mutation.isPending}
              onClick={() => mutation.mutate("accept")}
            >
              接收需求
            </Button>
          )}
        </div>
        {!adminPath && requirement.status === "dispatched" && (
          <div className="space-y-2">
            <label className="block space-y-1">
              <span>澄清说明</span>
              <Textarea
                disabled={mutation.isPending}
                value={note}
                onChange={(e) => setNote(e.target.value)}
              />
            </label>
            <Button
              variant="outline"
              disabled={mutation.isPending || !note.trim()}
              onClick={() => mutation.mutate("clarify")}
            >
              请求澄清
            </Button>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
