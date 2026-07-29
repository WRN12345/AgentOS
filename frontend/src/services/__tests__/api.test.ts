/**
 * newIdempotencyKey 的安全上下文回退（内网 http://<IP> 部署时 crypto.randomUUID 不可用）。
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { newIdempotencyKey } from "../api";

const UUID_V4_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

describe("newIdempotencyKey", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("安全上下文下使用 crypto.randomUUID", () => {
    const spy = vi
      .spyOn(crypto, "randomUUID")
      .mockReturnValue("00000000-0000-4000-8000-000000000000");
    expect(newIdempotencyKey()).toBe("00000000-0000-4000-8000-000000000000");
    expect(spy).toHaveBeenCalledOnce();
    spy.mockRestore();
  });

  it("非安全上下文（randomUUID 不存在）回退手拼 UUID v4", () => {
    // 模拟 http://内网IP 环境：randomUUID 为 undefined，getRandomValues 仍可用
    vi.stubGlobal("crypto", {
      getRandomValues: (arr: Uint8Array) => {
        for (let i = 0; i < arr.length; i += 1) arr[i] = (i * 37 + 11) & 0xff;
        return arr;
      },
    });
    const key = newIdempotencyKey();
    expect(key).toMatch(UUID_V4_RE);
  });

  it("回退实现每次生成不同的键", () => {
    vi.stubGlobal("crypto", globalThis.crypto && {
      getRandomValues: (arr: Uint8Array) => {
        for (let i = 0; i < arr.length; i += 1) arr[i] = Math.floor(Math.random() * 256);
        return arr;
      },
    });
    expect(newIdempotencyKey()).not.toBe(newIdempotencyKey());
  });
});
