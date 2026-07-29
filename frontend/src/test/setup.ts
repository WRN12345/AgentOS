import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, beforeEach } from "vitest";
import { useAuthStore } from "../app/store";

/**
 * 测试全局 setup：
 * 1. 注册 jest-dom 断言（toBeInTheDocument 等）；
 * 2. 补齐 jsdom 缺失、但 Radix UI（Select/Dialog/DropdownMenu）依赖的浏览器 API；
 * 3. 每个用例后卸载组件、清空登录态与 localStorage，避免用例间串扰。
 */

// jsdom 未实现 Pointer Capture 与 scrollIntoView，Radix Select/Slider 交互需要
Element.prototype.scrollIntoView = () => {};
if (!Element.prototype.hasPointerCapture) {
  Element.prototype.hasPointerCapture = () => false;
}
if (!Element.prototype.setPointerCapture) {
  Element.prototype.setPointerCapture = () => {};
}
if (!Element.prototype.releasePointerCapture) {
  Element.prototype.releasePointerCapture = () => {};
}

// jsdom 无 ResizeObserver（Radix Popper/Toast 尺寸观测需要）
class ResizeObserverMock {
  observe() {}
  unobserve() {}
  disconnect() {}
}
globalThis.ResizeObserver =
  ResizeObserverMock as unknown as typeof ResizeObserver;

// jsdom 无 matchMedia（sonner / next-themes 等可能探测）
if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  })) as typeof window.matchMedia;
}

beforeEach(() => {
  localStorage.clear();
  useAuthStore.getState().clear();
});

afterEach(() => {
  cleanup();
  localStorage.clear();
  useAuthStore.getState().clear();
});
