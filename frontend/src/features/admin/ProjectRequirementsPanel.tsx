import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { api, errorMessage, newIdempotencyKey } from "../../services/api";
import { queryKeys } from "../../lib/queryKeys";
import { formatDateTime } from "../work-items/constants";
import { RequirementCard } from "../project-requirements/RequirementCard";
import type {
  Analysis,
  Material,
  Requirement,
} from "../project-requirements/types";

const analysisStatus = {
  pending: "等待分析",
  running: "正在分析",
  succeeded: "分析完成",
  failed: "分析失败",
};

export default function ProjectRequirementsPanel({
  projectId,
}: {
  projectId: string;
}) {
  const client = useQueryClient();
  const base = `/admin/projects/${encodeURIComponent(projectId)}`;
  const key = queryKeys.adminRequirements(projectId);
  const requirementsKey = queryKeys.adminRequirements(
    projectId,
    "requirements",
  );
  const [selected, setSelected] = useState<string[]>([]);
  const [file, setFile] = useState<File | null>(null);
  const [fileKey, setFileKey] = useState(0);
  const [pollStart, setPollStart] = useState(Date.now);
  const [expired, setExpired] = useState(false);
  useEffect(() => {
    setExpired(false);
    const timer = window.setTimeout(
      () => setExpired(true),
      Math.max(0, pollStart + 300_000 - Date.now()),
    );
    return () => window.clearTimeout(timer);
  }, [pollStart]);
  const materials = useQuery({
    queryKey: [...key, "materials"],
    queryFn: () => api.get<Material[]>(`${base}/materials`),
  });
  const analyses = useQuery({
    queryKey: [...key, "analyses"],
    queryFn: () => api.get<Analysis[]>(`${base}/requirement-analyses`),
    refetchInterval: (query) =>
      !expired &&
      query.state.status !== "error" &&
      query.state.data?.some(
        (a) => a.status === "pending" || a.status === "running",
      )
        ? 2000
        : false,
    retry: false,
  });
  const requirements = useQuery({
    queryKey: requirementsKey,
    queryFn: () => api.get<Requirement[]>(`${base}/requirements`),
  });
  const completed = analyses.data
    ?.filter((a) => a.status === "succeeded")
    .map((a) => a.id)
    .sort()
    .join(",");
  useEffect(() => {
    if (completed)
      void client.invalidateQueries({
        queryKey: queryKeys.adminRequirements(projectId, "requirements"),
      });
  }, [completed, client, projectId]);
  const upload = useMutation({
    mutationFn: (value: File) => {
      const data = new FormData();
      data.append("file", value);
      return api.upload<Material>(
        `${base}/materials`,
        data,
        undefined,
        newIdempotencyKey(),
      );
    },
    onSuccess: (material) => {
      setFile(null);
      setFileKey((k) => k + 1);
      setSelected((ids) => [...ids, material.id]);
      void client.invalidateQueries({ queryKey: [...key, "materials"] });
    },
  });
  const analyze = useMutation({
    mutationFn: () =>
      api.post<Analysis>(
        `${base}/requirement-analyses`,
        { material_ids: selected },
        newIdempotencyKey(),
      ),
    onSuccess: () => {
      setPollStart(Date.now());
      void client.invalidateQueries({ queryKey: [...key, "analyses"] });
    },
  });
  const download = useMutation({
    mutationFn: async (material: Material) => {
      const result = await api.downloadFile(
        `${base}/materials/${material.id}/download`,
      );
      const url = URL.createObjectURL(result.blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = material.original_filename;
      anchor.click();
      URL.revokeObjectURL(url);
    },
  });
  const active = analyses.data?.some(
    (a) => a.status === "pending" || a.status === "running",
  );
  const supported = file && /\.(txt|md|pdf|docx)$/i.test(file.name);
  const refresh = () => {
    setPollStart(Date.now());
    void client.invalidateQueries({ queryKey: key });
  };
  return (
    <section aria-label="项目需求管理" className="space-y-4">
      <Card>
        <CardHeader className="gap-3">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <CardTitle>项目需求管理</CardTitle>
            <Button variant="outline" onClick={refresh}>
              刷新需求
            </Button>
          </div>
          <CardDescription>
            上传项目材料，分析并确认需求后派发给项目负责人。
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-5">
          <div className="space-y-2">
            <label className="block space-y-2">
              <span className="text-sm font-medium">上传需求材料</span>
              <Input
                key={fileKey}
                type="file"
                accept=".txt,.md,.pdf,.docx"
                disabled={upload.isPending}
                onChange={(e) => setFile(e.target.files?.[0] ?? null)}
              />
            </label>
            <p className="text-xs text-muted-foreground">
              支持 TXT、Markdown、PDF、DOCX；PDF 需包含文本，扫描件不支持。
            </p>
            {file && !supported && (
              <p role="alert" className="text-sm text-destructive">
                请选择支持的文件格式
              </p>
            )}
            <Button
              disabled={!supported || upload.isPending}
              onClick={() => file && upload.mutate(file)}
            >
              {upload.isPending ? "正在上传..." : "上传材料"}
            </Button>
          </div>
          {[
            { label: "材料", query: materials },
            { label: "分析记录", query: analyses },
            { label: "需求", query: requirements },
          ].map(({ label, query }) => (
            <div key={label} className="empty:hidden">
              {query.isPending && (
                <p role="status" className="text-sm">
                  正在加载{label}...
                </p>
              )}
              {query.isError && (
                <div role="alert" className="text-sm text-destructive">
                  {errorMessage(query.error, `${label}加载失败`)}
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => {
                      setPollStart(Date.now());
                      void query.refetch();
                    }}
                  >
                    重试{label}
                  </Button>
                </div>
              )}
            </div>
          ))}
          {materials.data && (
            <fieldset className="space-y-2">
              <legend className="mb-2 text-sm font-medium">选择分析材料</legend>
              {materials.data.length === 0 && (
                <p className="text-sm text-muted-foreground">暂无材料</p>
              )}
              {materials.data.map((material) => (
                <div
                  key={material.id}
                  className="flex flex-wrap items-center justify-between gap-2 rounded-md border p-3 text-sm"
                >
                  <label className="flex min-w-0 items-center gap-2">
                    <input
                      type="checkbox"
                      checked={selected.includes(material.id)}
                      onChange={(e) =>
                        setSelected((ids) =>
                          e.target.checked
                            ? [...ids, material.id]
                            : ids.filter((id) => id !== material.id),
                        )
                      }
                    />
                    <span className="break-all">
                      {material.original_filename} ·{" "}
                      {Math.ceil(material.size_bytes / 1024)} KB
                    </span>
                  </label>
                  <Button
                    variant="ghost"
                    size="sm"
                    disabled={download.isPending}
                    onClick={() => download.mutate(material)}
                  >
                    下载 {material.original_filename}
                  </Button>
                </div>
              ))}
            </fieldset>
          )}
          <p className="text-xs text-muted-foreground">
            所选材料将发送至已配置的 AI
            服务提供方。分析结果及建议验收标准需经人工核实。
          </p>
          <Button
            disabled={
              !selected.length ||
              analyze.isPending ||
              !!active ||
              materials.isError ||
              analyses.isPending ||
              analyses.isError
            }
            onClick={() => analyze.mutate()}
          >
            {analyze.isPending ? "正在提交..." : "分析所选材料"}
          </Button>
          {[upload, analyze, download].map(
            (mutation, i) =>
              mutation.isError && (
                <p key={i} role="alert" className="text-sm text-destructive">
                  {errorMessage(mutation.error, "操作失败，请重试")}
                </p>
              ),
          )}
          <div className="space-y-2">
            {analyses.data?.map((analysis) => (
              <div key={analysis.id} className="rounded-md border p-3 text-sm">
                <p
                  role={
                    analysis.status === "pending" ||
                    analysis.status === "running"
                      ? "status"
                      : undefined
                  }
                >
                  {analysisStatus[analysis.status]} ·{" "}
                  {formatDateTime(analysis.created_at)}
                </p>
                {analysis.status === "failed" && (
                  <div className="mt-2 space-y-2">
                    <p role="alert" className="text-destructive">
                      {analysis.error || "分析失败"}
                    </p>
                    <p className="text-xs text-muted-foreground">
                      选择材料后重新分析。
                    </p>
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={
                        !selected.length ||
                        analyze.isPending ||
                        !!active ||
                        materials.isError ||
                        analyses.isError
                      }
                      onClick={() => analyze.mutate()}
                    >
                      重新分析所选材料
                    </Button>
                  </div>
                )}
              </div>
            ))}
          </div>
          {expired && active && (
            <p role="status" className="text-sm text-muted-foreground">
              分析仍在进行，自动刷新已暂停。请点击刷新需求查看最新结果。
            </p>
          )}
        </CardContent>
      </Card>
      {requirements.data?.length === 0 && (
        <p className="text-sm text-muted-foreground">暂无需求草稿</p>
      )}
      {requirements.data?.map((requirement) => (
        <RequirementCard
          key={requirement.id}
          requirement={requirement}
          adminPath={`${base}/requirements`}
          onChanged={(updated) => {
            // Cancel reads started before the write so they cannot restore an old version.
            void client.cancelQueries({
              queryKey: requirementsKey,
              exact: true,
            });
            client.setQueryData<Requirement[]>(requirementsKey, (items) =>
              items?.map((item) =>
                item.id === updated.id && item.version <= updated.version
                  ? updated
                  : item,
              ),
            );
            void client.invalidateQueries({
              queryKey: requirementsKey,
              exact: true,
            });
          }}
        />
      ))}
    </section>
  );
}
