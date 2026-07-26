import { useAuthStore } from "../app/store";
import type { ApiErrorBody, TokenPair } from "../types";

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

  /** 是否为 409 版本冲突（WORK_ITEM_VERSION_CONFLICT 等）。 */
  get isVersionConflict(): boolean {
    return this.status === 409;
  }
}

const BASE_URL = "/api/v1";

/** 版本冲突提示文案（T2.7 验收要求）。 */
export const VERSION_CONFLICT_MESSAGE =
  "任务已被其他成员更新，请刷新后重试";

/** 生成幂等键：每次用户点击生成一个，同一操作的自动重试复用同一键。 */
export function newIdempotencyKey(): string {
  return crypto.randomUUID();
}

/** 从未知错误中提取用户可读文案。 */
export function errorMessage(error: unknown, fallback = "操作失败，请稍后重试"): string {
  if (error instanceof ApiError) {
    return error.message;
  }
  return fallback;
}

/** 并发刷新单例：多个请求同时 401 时只发起一次 refresh。 */
let refreshing: Promise<boolean> | null = null;

/** 尝试用 Refresh Token 换新令牌对；失败则清空登录态并跳转登录页。 */
function tryRefreshToken(): Promise<boolean> {
  if (!refreshing) {
    refreshing = (async () => {
      const { refreshToken, setTokens } = useAuthStore.getState();
      if (!refreshToken) {
        return false;
      }
      try {
        const response = await fetch(`${BASE_URL}/auth/refresh`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ refresh_token: refreshToken }),
        });
        if (!response.ok) {
          return false;
        }
        setTokens((await response.json()) as TokenPair);
        return true;
      } catch {
        return false;
      }
    })().then((ok) => {
      refreshing = null;
      if (!ok) {
        // 刷新失败：登录态已失效，清空并回到登录页
        useAuthStore.getState().clear();
        if (window.location.pathname !== "/login") {
          window.location.assign("/login");
        }
      }
      return ok;
    });
  }
  return refreshing;
}

interface RequestOptions {
  /** 幂等键（写操作/命令类接口携带）。 */
  idempotencyKey?: string;
  /** 内部标记：刷新重试仅允许一次。 */
  retried?: boolean;
}

async function request<T>(
  path: string,
  init: RequestInit = {},
  options: RequestOptions = {},
): Promise<T> {
  const token = useAuthStore.getState().accessToken;
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(init.headers as Record<string, string> | undefined),
  };
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }
  if (options.idempotencyKey) {
    headers["Idempotency-Key"] = options.idempotencyKey;
  }

  const response = await fetch(`${BASE_URL}${path}`, { ...init, headers });

  // Access Token 过期：刷新后重试一次
  if (response.status === 401 && token && !options.retried) {
    if (await tryRefreshToken()) {
      return request<T>(path, init, { ...options, retried: true });
    }
  }

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

/** 统一 API 客户端：自动拼接 /api/v1 前缀、携带认证头与幂等键、401 自动刷新重试、解析统一错误格式。 */
export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown, idempotencyKey?: string) =>
    request<T>(
      path,
      { method: "POST", body: JSON.stringify(body ?? {}) },
      { idempotencyKey },
    ),
  patch: <T>(path: string, body?: unknown, idempotencyKey?: string) =>
    request<T>(
      path,
      { method: "PATCH", body: JSON.stringify(body ?? {}) },
      { idempotencyKey },
    ),
  put: <T>(path: string, body?: unknown, idempotencyKey?: string) =>
    request<T>(
      path,
      { method: "PUT", body: JSON.stringify(body ?? {}) },
      { idempotencyKey },
    ),
  delete: <T>(path: string) => request<T>(path, { method: "DELETE" }),
};
