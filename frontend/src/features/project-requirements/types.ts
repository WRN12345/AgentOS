export interface Material {
  id: string;
  original_filename: string;
  size_bytes: number;
  created_at: string;
}

export interface Analysis {
  id: string;
  status: "pending" | "running" | "succeeded" | "failed";
  error: string | null;
  created_at: string;
}

export interface Requirement {
  id: string;
  title: string;
  description: string;
  acceptance_criteria: string;
  clarification_questions: string;
  sources: {
    material_id: string;
    filename: string;
    chunk_id: string;
    quote: string;
  }[];
  status:
    | "draft"
    | "confirmed"
    | "excluded"
    | "dispatched"
    | "accepted"
    | "clarification_requested";
  version: number;
  assignee_id: string | null;
  leader_note: string | null;
  discussion: {
    id: string;
    author_id: string | null;
    author_role: "admin" | "leader";
    body: string;
    created_at: string | null;
    version: number | null;
  }[];
  created_at: string;
  updated_at: string;
}

export const requirementStatus: Record<Requirement["status"], string> = {
  draft: "草稿",
  confirmed: "已确认",
  excluded: "已排除",
  dispatched: "已派发",
  accepted: "已接收",
  clarification_requested: "待澄清",
};
