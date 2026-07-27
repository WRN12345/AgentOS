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
  /**
   * multipart 文件上传（阶段 4，POST /files）：body 为 FormData，不设 Content-Type
   * （浏览器自动生成 boundary）；用 XMLHttpRequest 以支持 onprogress 上传进度；
   * 401 时刷新令牌重试一次，与 JSON 请求一致。
   */
  upload: <T>(
    path: string,
    formData: FormData,
    onProgress?: (percent: number) => void,
    idempotencyKey?: string,
  ) => upload<T>(path, formData, onProgress, { idempotencyKey }),
  /**
   * 鉴权文件下载（GET /files/{id}/download）：返回 blob 与文件名，
   * 权限不足/文件不存在时抛 ApiError（供 UI 提示 403）。
   */
  downloadFile: (path: string) => downloadFile(path),
};

/** XHR 上传实现：fetch 不支持上传进度事件。 */
function upload<T>(
  path: string,
  formData: FormData,
  onProgress: ((percent: number) => void) | undefined,
  options: RequestOptions,
  retried = false,
): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const token = useAuthStore.getState().accessToken;
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${BASE_URL}${path}`);
    if (token) {
      xhr.setRequestHeader("Authorization", `Bearer ${token}`);
    }
    if (options.idempotencyKey) {
      xhr.setRequestHeader("Idempotency-Key", options.idempotencyKey);
    }
    if (onProgress) {
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable && e.total > 0) {
          onProgress(Math.round((e.loaded / e.total) * 100));
        }
      };
    }
    xhr.onload = () => {
      if (xhr.status === 401 && token && !retried) {
        // Access Token 过期：刷新后重试一次
        tryRefreshToken().then((ok) => {
          if (ok) {
            upload<T>(path, formData, onProgress, options, true).then(
              resolve,
              reject,
            );
          } else {
            reject(
              new ApiError(401, {
                code: "UNAUTHORIZED",
                message: "登录已过期，请重新登录",
                request_id: "",
              }),
            );
          }
        });
        return;
      }
      let body: unknown;
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        body = undefined;
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(body as T);
        return;
      }
      const errorBody = body as ApiErrorBody | undefined;
      reject(
        new ApiError(
          xhr.status,
          errorBody && typeof errorBody.code === "string"
            ? errorBody
            : {
                code: `HTTP_${xhr.status}`,
                message: xhr.statusText || "上传失败",
                request_id: "",
              },
        ),
      );
    };
    xhr.onerror = () => {
      reject(
        new ApiError(0, {
          code: "NETWORK_ERROR",
          message: "网络错误，上传中断",
          request_id: "",
        }),
      );
    };
    xhr.send(formData);
  });
}

/** 鉴权下载：复用 401 刷新逻辑，成功后从 Content-Disposition 解析文件名。 */
async function downloadFile(
  path: string,
  retried = false,
): Promise<{ blob: Blob; filename: string }> {
  const token = useAuthStore.getState().accessToken;
  const headers: Record<string, string> = {};
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }
  const response = await fetch(`${BASE_URL}${path}`, { headers });

  if (response.status === 401 && token && !retried) {
    if (await tryRefreshToken()) {
      return downloadFile(path, true);
    }
  }

  if (!response.ok) {
    let body: ApiErrorBody | undefined;
    try {
      body = (await response.json()) as ApiErrorBody;
    } catch {
      body = undefined;
    }
    throw new ApiError(
      response.status,
      body && typeof body.code === "string"
        ? body
        : {
            code: `HTTP_${response.status}`,
            message: response.statusText || "下载失败",
            request_id: "",
          },
    );
  }

  // RFC 5987：优先 filename*（UTF-8 编码名），否则取 ASCII 兜底 filename
  const disposition = response.headers.get("Content-Disposition") ?? "";
  const starMatch = /filename\*=UTF-8''([^;]+)/i.exec(disposition);
  const plainMatch = /filename="?([^";]+)"?/i.exec(disposition);
  const filename = starMatch
    ? decodeURIComponent(starMatch[1])
    : (plainMatch?.[1] ?? "download");
  return { blob: await response.blob(), filename };
}
