import path from "path";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// 前端测试配置（T6.2 / 设计文档 18.2 节）：
// jsdom 环境 + @testing-library，setup 中注入 jest-dom 断言与 Radix 所需的浏览器 API polyfill。
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    // Radix 组件会渲染到 document.body 的 portal，测试中需要全局清理
    globals: false,
    css: false,
  },
});
