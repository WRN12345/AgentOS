import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

vi.mock("../../../services/api", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("../../../services/api")>();
  const { mockApi } = await import("../../../test/mock-api");
  return { ...actual, api: mockApi };
});

import { toast } from "sonner";
import { ApiError } from "../../../services/api";
import { WorkItemFormDialog } from "../work-item-form";
import { mockApi } from "../../../test/mock-api";
import { renderWithProviders } from "../../../test/render";
import { makeMember, makeWorkItem } from "../../../test/fixtures";

const members = [
  makeMember({ id: "member-1", display_name: "爱丽丝" }),
  makeMember({ id: "member-2", display_name: "鲍勃", username: "bob" }),
  // 停用成员不应出现在主执行人候选中
  makeMember({ id: "member-3", display_name: "已停用", is_active: false }),
];

/** 打开 Radix Select 并选中指定文案的选项。 */
async function pickSelectOption(
  user: ReturnType<typeof userEvent.setup>,
  trigger: HTMLElement,
  optionName: string,
) {
  await user.click(trigger);
  const listbox = await screen.findByRole("listbox");
  await user.click(within(listbox).getByText(optionName));
}

/** 创建工作项表单组件测试（18.2 节）：渲染、必填校验、提交调用 API、错误展示。 */
describe("WorkItemFormDialog 创建工作项表单", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("渲染标题、优先级、主执行人等字段，停用成员不在候选中", () => {
    renderWithProviders(
      <WorkItemFormDialog open onOpenChange={() => {}} members={members} />,
    );
    expect(screen.getByText("创建工作项")).toBeInTheDocument();
    expect(screen.getByLabelText("标题")).toBeInTheDocument();
    expect(screen.getByText("主执行人")).toBeInTheDocument();
    expect(screen.queryByText("已停用")).not.toBeInTheDocument();
  });

  it("必填校验：空标题与未选主执行人时展示错误且不调用接口", async () => {
    const user = userEvent.setup();
    renderWithProviders(
      <WorkItemFormDialog open onOpenChange={() => {}} members={members} />,
    );

    await user.click(screen.getByRole("button", { name: "创建" }));

    expect(await screen.findByText("请输入标题")).toBeInTheDocument();
    expect(screen.getByText("请选择主执行人")).toBeInTheDocument();
    expect(mockApi.post).not.toHaveBeenCalled();
  });

  it("填写完整后提交，调用 POST /work-items 并关闭对话框", async () => {
    const user = userEvent.setup();
    const onOpenChange = vi.fn();
    mockApi.post.mockResolvedValue(makeWorkItem());

    renderWithProviders(
      <WorkItemFormDialog open onOpenChange={onOpenChange} members={members} />,
    );

    await user.type(screen.getByLabelText("标题"), "搭建 RAG 检索管道");
    const comboboxes = screen.getAllByRole("combobox");
    // 第二个 Select 是主执行人（第一个为优先级）
    await pickSelectOption(user, comboboxes[1], "爱丽丝");
    await user.click(screen.getByRole("button", { name: "创建" }));

    await waitFor(() => {
      expect(mockApi.post).toHaveBeenCalledWith(
        "/work-items",
        expect.objectContaining({
          title: "搭建 RAG 检索管道",
          priority: "medium",
          assignee_id: "member-1",
          collaborator_ids: [],
          due_at: null,
        }),
        expect.any(String), // 幂等键
      );
    });
    await waitFor(() => {
      expect(toast.success).toHaveBeenCalledWith("工作项已创建");
      expect(onOpenChange).toHaveBeenCalledWith(false);
    });
  });

  it("接口报错时展示后端错误文案", async () => {
    const user = userEvent.setup();
    mockApi.post.mockRejectedValue(
      new ApiError(400, {
        code: "VALIDATION",
        message: "主执行人不存在",
        request_id: "req-1",
      }),
    );

    renderWithProviders(
      <WorkItemFormDialog open onOpenChange={() => {}} members={members} />,
    );
    await user.type(screen.getByLabelText("标题"), "任务");
    const comboboxes = screen.getAllByRole("combobox");
    await pickSelectOption(user, comboboxes[1], "鲍勃");
    await user.click(screen.getByRole("button", { name: "创建" }));

    await waitFor(() => {
      expect(toast.error).toHaveBeenCalledWith("主执行人不存在");
    });
  });

  it("编辑模式携带版本号走 PATCH，409 冲突时提示刷新", async () => {
    const user = userEvent.setup();
    const workItem = makeWorkItem({
      id: "wi-1",
      title: "旧标题",
      version: 3,
      assignee: { id: "member-1", display_name: "爱丽丝" },
    });
    mockApi.patch.mockRejectedValue(
      new ApiError(409, {
        code: "WORK_ITEM_VERSION_CONFLICT",
        message: "版本冲突",
        request_id: "req-2",
      }),
    );

    renderWithProviders(
      <WorkItemFormDialog
        open
        onOpenChange={() => {}}
        members={members}
        workItem={workItem}
      />,
    );

    const titleInput = screen.getByLabelText("标题");
    await user.clear(titleInput);
    await user.type(titleInput, "新标题");
    await user.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => {
      expect(mockApi.patch).toHaveBeenCalledWith(
        "/work-items/wi-1",
        expect.objectContaining({ version: 3, title: "新标题" }),
        expect.any(String),
      );
    });
    await waitFor(() => {
      expect(toast.error).toHaveBeenCalledWith(
        "任务已被其他成员更新，请刷新后重试",
      );
    });
  });
});
