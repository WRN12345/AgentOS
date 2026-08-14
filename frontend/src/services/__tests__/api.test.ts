/**
 * newIdempotencyKey 的安全上下文回退（内网 http://<IP> 部署时 crypto.randomUUID 不可用）。
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { api, newIdempotencyKey } from "../api";
import { useAuthStore } from "../../app/store";
import { makeProject, tokens } from "../../test/fixtures";

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

describe("X-Project-Id 请求头", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    useAuthStore.getState().clear();
  });

  /** 桩掉 fetch，返回 200 JSON，记录调用参数。 */
  function stubFetch() {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  }

  it("普通请求：选定项目时自动携带 X-Project-Id", async () => {
    const fetchMock = stubFetch();
    useAuthStore.getState().setTokens(tokens);
    useAuthStore.getState().setCurrentProject(makeProject());

    await api.get("/work-items");

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(init.headers).toMatchObject({ "X-Project-Id": "project-1" });
  });

  it("普通请求：未选项目（全局 admin / 未分流）时不携带 X-Project-Id", async () => {
    const fetchMock = stubFetch();
    useAuthStore.getState().setTokens(tokens);

    await api.get("/work-items");

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(init.headers).not.toHaveProperty("X-Project-Id");
  });

  it("下载请求：选定项目时自动携带 X-Project-Id", async () => {
    const fetchMock = stubFetch();
    useAuthStore.getState().setTokens(tokens);
    useAuthStore.getState().setCurrentProject(makeProject());

    await api.downloadFile("/files/f1/download");

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(init.headers).toMatchObject({ "X-Project-Id": "project-1" });
  });

  it("上传请求：选定项目时 XHR 携带 X-Project-Id", async () => {
    useAuthStore.getState().setTokens(tokens);
    useAuthStore.getState().setCurrentProject(makeProject());

    class FakeXHR {
      upload = {
        onprogress: null as null | ((e: { lengthComputable: boolean; total: number }) => void),
      };
      status = 200;
      responseText = JSON.stringify({ id: "file-1" });
      onload: (() => void) | null = null;
      onerror: (() => void) | null = null;
      open = vi.fn();
      setRequestHeader = vi.fn();
      send = vi.fn();
    }
    const fake = new FakeXHR();
    vi.stubGlobal("XMLHttpRequest", vi.fn(() => fake));

    const form = new FormData();
    form.append("file", new Blob(["x"]), "a.txt");
    const pending = api.upload("/files", form);
    fake.onload?.();
    await pending;

    expect(fake.setRequestHeader).toHaveBeenCalledWith(
      "X-Project-Id",
      "project-1",
    );
  });
});
