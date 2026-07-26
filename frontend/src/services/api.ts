import { useAuthStore } from "../app/store";
import type { ApiErrorBody } from "../types";

/** 结构化 API 错误：携带统一错误格式中的 code / message / request_id / details。 */
export class ApiError extends Error {
  readonly code: string;
  readonly requestId: string;
  readonly details?: Record<string, unknown>;
  readonly status: number;

  constructor(status: number, body: ApiErrorBody) {
    super(body.message);
    this.name = "ApiError";
    this.status = status;
    this.code = body.code;
    this.requestId = body.request_id;
    this.details = body.details;
  }
}

const BASE_URL = "/api/v1";

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = useAuthStore.getState().token;
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(init.headers as Record<string, string> | undefined),
  };
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }

  const response = await fetch(`${BASE_URL}${path}`, { ...init, headers });

  if (!response.ok) {
    let body: ApiErrorBody | undefined;
    try {
      body = (await response.json()) as ApiErrorBody;
    } catch {
      body = undefined;
    }
    if (body && typeof body.code === "string") {
      throw new ApiError(response.status, body);
    }
    throw new ApiError(response.status, {
      code: `HTTP_${response.status}`,
      message: response.statusText || "请求失败",
      request_id: "",
      details: {},
    });
  }

  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

/** 统一 API 客户端：自动拼接 /api/v1 前缀、携带认证头、解析统一错误格式。 */
export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "POST", body: JSON.stringify(body ?? {}) }),
  patch: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "PATCH", body: JSON.stringify(body ?? {}) }),
  put: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "PUT", body: JSON.stringify(body ?? {}) }),
  delete: <T>(path: string) => request<T>(path, { method: "DELETE" }),
};
