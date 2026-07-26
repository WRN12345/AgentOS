/** API 统一错误格式（设计文档 17.1 节）。 */
export interface ApiErrorBody {
  code: string;
  message: string;
  request_id: string;
  details?: Record<string, unknown>;
}

export interface HealthResponse {
  status: string;
  checks: Record<string, string>;
}
