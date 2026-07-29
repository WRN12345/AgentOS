import { vi } from "vitest";

/**
 * 测试替身 API 客户端。
 *
 * 用法：测试文件中用 vi.mock 替换 src/services/api 模块的 api 对象
 * （保留 ApiError 等真实导出），例如：
 *
 *   vi.mock("../../../services/api", async (importOriginal) => {
 *     const actual =
 *       await importOriginal<typeof import("../../../services/api")>();
 *     const { mockApi } = await import("../../test/mock-api");
 *     return { ...actual, api: mockApi };
 *   });
 *
 * 之后在 beforeEach 中 vi.clearAllMocks() 并按路径配置返回值。
 */
export const mockApi = {
  get: vi.fn(),
  post: vi.fn(),
  patch: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
  upload: vi.fn(),
  downloadFile: vi.fn(),
};

/** 按请求路径配置 GET 返回值；未命中的路径返回空数组（列表接口的安全默认）。 */
export function stubGet(map: Record<string, unknown>) {
  mockApi.get.mockImplementation((path: string) => {
    for (const [prefix, value] of Object.entries(map)) {
      if (path === prefix || path.startsWith(prefix)) {
        return Promise.resolve(value);
      }
    }
    return Promise.resolve([]);
  });
}
