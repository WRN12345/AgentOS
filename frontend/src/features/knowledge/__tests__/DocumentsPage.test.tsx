import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

vi.mock("../../../services/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../services/api")>();
  const { mockApi } = await import("../../../test/mock-api");
  return { ...actual, api: mockApi };
});

import DocumentsPage from "../DocumentsPage";
import { mockApi } from "../../../test/mock-api";
import { createTestQueryClient, renderWithProviders } from "../../../test/render";
import { queryKeys } from "../../../lib/queryKeys";
import { ApiError } from "../../../services/api";
import type { StoredFile } from "../../../types";

const rootFile: StoredFile = {
  id: "file-root",
  original_filename: "说明.md",
  directory_path: "/",
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
const nestedFile: StoredFile = {
  ...rootFile,
  id: "file-nested",
  directory_path: "/资料 & 设计",
  index_status: "failed",
};

describe("DocumentsPage 目录", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    mockApi.get.mockImplementation((path: string) => {
      if (path === `/files/${nestedFile.id}/versions`) return Promise.resolve([nestedFile]);
      const directory = new URL(path, "http://localhost").searchParams.get("directory_path");
      return Promise.resolve(
        [rootFile, nestedFile].filter((file) => directory === null || file.directory_path === directory),
      );
    });
  });

  it("默认显示全部目录，提交后精确筛选并按目录隔离缓存，清空恢复全部", async () => {
    const user = userEvent.setup();
    const queryClient = createTestQueryClient();
    queryClient.setDefaultOptions({ queries: { retry: false, gcTime: Infinity } });
    renderWithProviders(<DocumentsPage />, { queryClient });
    expect(await screen.findAllByText("说明.md")).toHaveLength(2);
    expect(mockApi.get).toHaveBeenCalledWith("/files");
    expect(screen.getByRole("columnheader", { name: "目录" })).toBeInTheDocument();
    expect(screen.getByText(nestedFile.directory_path)).toBeInTheDocument();
    expect(screen.getByText("同目录同名重新上传生成新版本，旧版本保留可查")).toBeInTheDocument();

    const input = screen.getByLabelText("目录筛选");
    await user.type(input, nestedFile.directory_path);
    expect(mockApi.get).toHaveBeenCalledTimes(1);
    await user.click(screen.getByRole("button", { name: "应用筛选" }));
    const nestedPath = `/files?${new URLSearchParams({ directory_path: nestedFile.directory_path })}`;
    await waitFor(() => expect(mockApi.get).toHaveBeenCalledWith(nestedPath));
    await waitFor(() => expect(screen.getAllByText("说明.md")).toHaveLength(1));
    expect(queryClient.getQueryData(queryKeys.files("current", ""))).toEqual([rootFile, nestedFile]);
    expect(queryClient.getQueryData(queryKeys.files("current", nestedFile.directory_path))).toEqual([nestedFile]);

    await user.clear(input);
    await user.type(input, "/{Enter}");
    await waitFor(() => expect(mockApi.get).toHaveBeenCalledWith("/files?directory_path=%2F"));
    await waitFor(() => expect(screen.queryByText(nestedFile.directory_path)).not.toBeInTheDocument());
    expect(await screen.findByText("说明.md")).toBeInTheDocument();

    await user.clear(input);
    await user.click(screen.getByRole("button", { name: "应用筛选" }));
    expect(await screen.findAllByText("说明.md")).toHaveLength(2);
  });

  it("空目录展示空态", async () => {
    renderWithProviders(<DocumentsPage />);
    const user = userEvent.setup();
    await screen.findAllByText("说明.md");
    await user.type(screen.getByLabelText("目录筛选"), "/empty{Enter}");
    expect(await screen.findByText("暂无文档")).toBeInTheDocument();
  });

  it.each([
    { name: "反斜杠", value: "/a\\b", message: "目录不能包含反斜杠或 NUL 字符" },
    { name: "NUL", value: "/a\0b", message: "目录不能包含反斜杠或 NUL 字符" },
    { name: "点路径段", value: "/a/./b", message: "目录不能包含 . 或 .. 路径段" },
    { name: "父目录路径段", value: "/../x", message: "目录不能包含 . 或 .. 路径段" },
    { name: "末尾父目录", value: "a/..", message: "目录不能包含 . 或 .. 路径段" },
    { name: "规范化后超长", value: "a".repeat(512), message: "规范化后的目录不能超过 512 个字符" },
  ])("拒绝 $name，保留已应用筛选且不发起请求", async ({ value, message }) => {
    const user = userEvent.setup();
    renderWithProviders(<DocumentsPage />);
    await screen.findAllByText("说明.md");
    const input = screen.getByLabelText("目录筛选");
    await user.type(input, "/{Enter}");
    await waitFor(() => expect(screen.getAllByText("说明.md")).toHaveLength(1));
    mockApi.get.mockClear();

    fireEvent.change(input, { target: { value } });
    await user.click(screen.getByRole("button", { name: "应用筛选" }));
    expect(await screen.findByText(message)).toBeInTheDocument();
    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(mockApi.get).not.toHaveBeenCalled();
    expect(screen.getAllByText("说明.md")).toHaveLength(1);
    expect(screen.queryByText("暂无文档")).not.toBeInTheDocument();
  });

  it.each([
    { name: "相对目录与重复斜杠", value: "资料//设计/", canonical: "/资料/设计" },
    { name: "根目录", value: "///", canonical: "/" },
    { name: "含点名称", value: "/.hidden/a..b", canonical: "/.hidden/a..b" },
    { name: "目录中的空格", value: " /资料 ", canonical: "/ /资料 " },
    { name: "规范化长度边界", value: `///${"a".repeat(511)}///`, canonical: `/${"a".repeat(511)}` },
    { name: "Unicode 字符长度", value: "\u{20000}".repeat(511), canonical: `/${"\u{20000}".repeat(511)}` },
  ])("按后端规则规范化 $name，保留输入草稿", async ({ value, canonical }) => {
    const user = userEvent.setup();
    const { queryClient } = renderWithProviders(<DocumentsPage />);
    await screen.findAllByText("说明.md");
    const input = screen.getByLabelText("目录筛选");
    fireEvent.change(input, { target: { value } });
    expect(mockApi.get).toHaveBeenCalledTimes(1);
    await user.click(screen.getByRole("button", { name: "应用筛选" }));
    await waitFor(() => expect(mockApi.get).toHaveBeenCalledWith(
      `/files?${new URLSearchParams({ directory_path: canonical })}`,
    ));
    await waitFor(() => expect(queryClient.getQueryState(queryKeys.files("current", canonical))?.status).toBe("success"));
    expect(input).toHaveValue(value);
    expect(input).toHaveAttribute("aria-invalid", "false");
  });

  it.each([
    {
      name: "后端校验错误",
      error: new ApiError(422, { code: "INVALID_DIRECTORY", message: "目录格式无效", request_id: "request-1" }),
      message: "目录格式无效",
    },
    { name: "网络错误", error: new Error("Failed to fetch"), message: "文档加载失败，请稍后重试" },
  ])("筛选查询发生$name时展示警告而非空态", async ({ error, message }) => {
    const user = userEvent.setup();
    renderWithProviders(<DocumentsPage />);
    await screen.findAllByText("说明.md");
    mockApi.get.mockRejectedValue(error);
    await user.type(screen.getByLabelText("目录筛选"), "/资料{Enter}");
    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(screen.queryByText("暂无文档")).not.toBeInTheDocument();
  });

  it("筛选后的文件仍可重试索引并查看该文件的版本历史", async () => {
    renderWithProviders(<DocumentsPage />);
    mockApi.post.mockResolvedValue(nestedFile);
    const user = userEvent.setup();
    await screen.findAllByText("说明.md");
    await user.type(screen.getByLabelText("目录筛选"), `${nestedFile.directory_path}{Enter}`);
    await waitFor(() => expect(screen.getAllByText("说明.md")).toHaveLength(1));
    const nestedPath = `/files?${new URLSearchParams({ directory_path: nestedFile.directory_path })}`;
    mockApi.get.mockClear();
    await user.click(screen.getByRole("button", { name: "重试索引" }));
    await waitFor(() => expect(mockApi.post).toHaveBeenCalledWith(`/files/${nestedFile.id}/index-retry`));
    await waitFor(() => expect(mockApi.get).toHaveBeenCalledWith(nestedPath));
    await user.click(screen.getByRole("button", { name: "版本历史" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/目录：\/资料 & 设计/)).toBeInTheDocument();
    await waitFor(() => expect(mockApi.get).toHaveBeenCalledWith(`/files/${nestedFile.id}/versions`));
    expect(await within(dialog).findByText("当前版本")).toBeInTheDocument();
  });
});
