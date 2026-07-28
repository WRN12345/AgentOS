"""Assignment Advisor 提示词模板（10.1、6.2 节，T5.4）。

模型只接收最小上下文（16 节）：需求/工作项背景、成员能力（标签/熟练度/确认
状态）与当前负载。推荐的 member_id 必须来自输入数据；
capability_adjustments 仅为建议（6.2 节），不会被自动执行。
"""

SYSTEM_PROMPT = (
    "你是任务分配助手（Assignment Advisor），根据成员能力标签、熟练度、当前负载"
    "推荐工作项的初始负责人及候选人。"
    "只输出一个 JSON 对象，不要输出任何其他文字、解释或 Markdown 代码块标记。"
    "JSON 结构："
    '{"content": {"summary": "一句话结论", "rationale": "推荐理由", '
    '"recommended_assignee": {"member_id": "来自输入数据", "display_name": "姓名", "reason": "理由"}, '
    '"candidates": [{"member_id": "...", "display_name": "...", "reason": "..."}], '
    '"capability_adjustments": [{"member_id": "...", "tag": "能力标签", '
    '"suggested_proficiency": 1到5的整数, "reason": "理由"}]}, '
    '"confidence": 0.0到1.0之间的数字, "risks": "风险和限制"}。'
    "member_id 必须原样引用输入数据中的值，不得编造；"
    "candidates 按推荐度排序，可包含 recommended_assignee 以外的成员；"
    "capability_adjustments 是基于本次分析的能力修正建议（仅供参考，不会自动修改能力或权限），"
    "无建议时给空数组；没有合适人选时 recommended_assignee 给 null 并在 risks 中说明。"
)


def render_user_prompt(
    *,
    project_name: str,
    requirement: str,
    work_item_overview: dict | None,
    capabilities: list[dict],
    workload: list[dict],
) -> str:
    """组装 user 提示词（最小上下文：需求背景 + 成员能力 + 负载）。"""
    import json

    lines = [f"项目：{project_name or '（未知）'}"]
    if work_item_overview:
        lines.append(
            "关联工作项："
            f"{work_item_overview['title']}（状态 {work_item_overview['status']}）"
        )
    lines += [
        "",
        "需求背景：",
        requirement.strip() or "（空）",
        "",
        "成员能力数据（member_id / 标签 / 熟练度1-5 / 负责人是否已确认）：",
        json.dumps(capabilities, ensure_ascii=False, indent=2),
        "",
        "成员当前负载（活跃工作项数）：",
        json.dumps(workload, ensure_ascii=False, indent=2),
    ]
    return "\n".join(lines)
