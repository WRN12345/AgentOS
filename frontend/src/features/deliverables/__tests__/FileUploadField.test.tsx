import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

vi.mock("../../../services/api", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("../../../services/api")>();
  const { mockApi } = await import("../../../test/mock-api");
  return { ...actual, api: mockApi };
});

import { FileUploadField } from "../FileUploadField";
import { mockApi } from "../../../test/mock-api";
import { renderWithProviders } from "../../../test/render";
import type { StoredFile } from "../../../types";

const storedFile: StoredFile = {
  id: "file-1",
  original_filename: "说明文档.md",
  size_bytes: 2048,
  mime_type: "text/markdown",
  sha256: "a".repeat(64),
  storage_backend: "local",
  uploaded_by: "member-1",
  work_item_id: "wi-1",
  version: 1,
  superseded_by: null,
  index_status: "indexed",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

function renderField(overrides: Partial<Parameters<typeof FileUploadField>[0]> = {}) {
  const props = {
    workItemId: "wi-1",
    onUploaded: vi.fn(),
    onClear: vi.fn(),
    ...overrides,
  };
  renderWithProviders(<FileUploadField {...props} />);
  return props;
}

function pickFile(input: HTMLElement, file: File) {
  // 合法文件走 userEvent.upload（模拟真实选择）
  return userEvent.setup().upload(input, file);
}

/**
 * 直接派发 change：userEvent.upload 会按 input 的 accept 属性过滤掉
 * 不匹配的文件（符合真实文件选择器行为），要验证"绕过选择器"的前置
 * 校验分支时需要用 fireEvent 注入文件。
 */
function injectFile(input: HTMLElement, file: File) {
  fireEvent.change(input, { target: { files: [file] } });
}

/** 文件上传组件测试（18.2 节）：前置校验、上传调用、成功/失败展示与重试。 */
describe("FileUploadField 文件上传", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("不支持的扩展名：展示校验错误且不发起上传", async () => {
    const props = renderField();
    const input = document.querySelector(
      'input[type="file"]',
    ) as HTMLInputElement;

    await injectFile(input, new File(["x"], "evil.exe", { type: "application/octet-stream" }));

    expect(
      await screen.findByText(/不支持的文件类型 \.exe/),
    ).toBeInTheDocument();
    expect(mockApi.upload).not.toHaveBeenCalled();
    expect(props.onClear).toHaveBeenCalled();
  });

  it("超过 20MB：展示大小超限错误且不发起上传", async () => {
    renderField();
    const input = document.querySelector(
      'input[type="file"]',
    ) as HTMLInputElement;
    const big = new File(["x"], "big.zip", { type: "application/zip" });
    Object.defineProperty(big, "size", { value: 21 * 1024 * 1024 });

    await pickFile(input, big);

    expect(await screen.findByText(/文件超过大小上限/)).toBeInTheDocument();
    expect(mockApi.upload).not.toHaveBeenCalled();
  });

  it("选择合法文件即上传：调用 /files 并展示「已上传」，回调回传文件记录", async () => {
    const props = renderField();
    mockApi.upload.mockResolvedValue(storedFile);
    const input = document.querySelector(
      'input[type="file"]',
    ) as HTMLInputElement;

    await pickFile(
      input,
      new File(["# 标题"], "说明文档.md", { type: "text/markdown" }),
    );

    await waitFor(() => {
      expect(mockApi.upload).toHaveBeenCalledWith(
        "/files",
        expect.any(FormData),
        expect.any(Function), // onprogress 回调
        expect.any(String), // 幂等键
      );
    });
    // FormData 携带文件与关联工作项
    const formData = mockApi.upload.mock.calls[0][1] as FormData;
    expect(formData.get("work_item_id")).toBe("wi-1");
    expect((formData.get("file") as File).name).toBe("说明文档.md");

    expect(await screen.findByText(/已上传/)).toBeInTheDocument();
    await waitFor(() => {
      expect(props.onUploaded).toHaveBeenCalledWith(storedFile);
    });
  });

  it("上传失败：展示错误信息与重试按钮，点击重试重新上传", async () => {
    renderField();
    mockApi.upload
      .mockRejectedValueOnce(new Error("网络错误"))
      .mockResolvedValueOnce(storedFile);
    const input = document.querySelector(
      'input[type="file"]',
    ) as HTMLInputElement;

    await pickFile(
      input,
      new File(["# 标题"], "说明文档.md", { type: "text/markdown" }),
    );

    // 失败后展示错误与重试入口（非 ApiError 时使用兜底文案）
    expect(
      await screen.findByText("上传失败，请重试"),
    ).toBeInTheDocument();

    await userEvent
      .setup()
      .click(screen.getByRole("button", { name: /重试/ }));

    await waitFor(() => {
      expect(mockApi.upload).toHaveBeenCalledTimes(2);
    });
    expect(await screen.findByText(/已上传/)).toBeInTheDocument();
  });
});
