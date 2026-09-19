import { useState } from "react";
import { Navigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useAuthStore, useIsLeader } from "../../app/store";
import { api, errorMessage } from "../../services/api";
import { queryKeys } from "../../lib/queryKeys";
import { Button } from "@/components/ui/button";
import { RequirementCard } from "./RequirementCard";
import type { Requirement } from "./types";
import type { AgentConfig, Member } from "../../types";
import { RequirementPipelineWizard } from "../agent-assistant/RequirementPipelineWizard";

export default function ProjectRequirementsPage() {
  const isLeader = useIsLeader();
  const projectId = useAuthStore((s) => s.currentProject?.id);
  const [selected, setSelected] = useState<{ projectId: string | undefined; id: string } | null>(null);
  const wizardOpen = selected !== null && selected.projectId === projectId;
  const { data: members } = useQuery({
    queryKey: queryKeys.members(),
    queryFn: () => api.get<Member[]>("/members"),
    enabled: isLeader && wizardOpen,
  });
  const { data: config } = useQuery({
    queryKey: ["config"],
    queryFn: () => api.get<AgentConfig>("/config"),
    enabled: isLeader && wizardOpen,
  });
  const client = useQueryClient();
  const requirementsKey = queryKeys.projectRequirements();
  const requirements = useQuery({
    queryKey: requirementsKey,
    queryFn: () => api.get<Requirement[]>("/project-requirements"),
    enabled: isLeader,
    refetchInterval: (query) => query.state.status === "error" ? false : 5000,
    refetchIntervalInBackground: false,
    retry: false,
  });
  const selectedRequirement = wizardOpen
    ? requirements.data?.find((item) => item.id === selected.id && item.status === "accepted")
    : undefined;
  if (!isLeader) return <Navigate to="/" replace />;
  return (
    <div className="mx-auto max-w-4xl space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold">项目需求</h1>
        <div className="flex flex-wrap gap-2">
          <Button
            variant="outline"
            disabled={requirements.isFetching}
            onClick={() => void requirements.refetch()}
          >
            刷新需求
          </Button>
        </div>
      </div>
      {requirements.isPending && <p role="status">正在加载需求...</p>}
      {requirements.isError && (
        <p role="alert" className="text-destructive">
          {errorMessage(requirements.error, "需求加载失败，请刷新重试")}
        </p>
      )}
      {requirements.data?.length === 0 && (
        <p className="text-sm text-muted-foreground">暂无派发给你的需求</p>
      )}
      {requirements.data?.map((requirement) => (
        <RequirementCard
          key={`${projectId}-${requirement.id}`}
          requirement={requirement}
          onDecompose={() => setSelected({ projectId, id: requirement.id })}
          onChanged={(updated) => {
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
      {selectedRequirement && (
        <RequirementPipelineWizard
          key={`${projectId}-${selectedRequirement.id}`}
          open
          onOpenChange={(open) => { if (!open) setSelected(null); }}
          members={members ?? []}
          llmIsExternal={config?.llm_is_external ?? false}
          projectRequirement={selectedRequirement}
        />
      )}
    </div>
  );
}
