import { QueryClient } from "@tanstack/react-query";

/** 应用级查询缓存；身份切换时由 session 统一清空，避免跨账号复用业务数据。 */
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
});
