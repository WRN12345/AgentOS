import type { ReactElement, ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { useAuthStore } from "../app/store";
import type { Member, UserMe } from "../types";
import { makeUser } from "./fixtures";

/** 构造测试用 QueryClient：关闭重试与垃圾回收延迟，避免异步噪音。 */
export function createTestQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0 },
      mutations: { retry: false },
    },
  });
}

interface RenderOptions {
  /** MemoryRouter 初始路由。 */
  route?: string;
  queryClient?: QueryClient;
}

/**
 * 以页面运行所需的 Provider 组合渲染组件：
 * react-query + MemoryRouter（页面内部普遍使用 Link/useNavigate）。
 */
export function renderWithProviders(
  ui: ReactElement,
  { route = "/", queryClient }: RenderOptions = {},
) {
  const client = queryClient ?? createTestQueryClient();
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[route]}>{children}</MemoryRouter>
    </QueryClientProvider>
  );
  return { queryClient: client, ...render(ui, { wrapper }) };
}

/** 写入登录态：令牌 + 用户 + 成员身份（角色决定权限差异）。 */
export function signInAs(member: Member, user?: UserMe): void {
  const store = useAuthStore.getState();
  store.setTokens({
    access_token: "test-access-token",
    refresh_token: "test-refresh-token",
    token_type: "bearer",
    expires_in: 1800,
  });
  store.setIdentity(
    user ?? makeUser({ id: member.user_id, username: member.username }),
    member,
  );
}
