"""Planning Advisor 提示词模板（10.1 节，T5.4）。

模型只接收最小上下文（16 节）：需求背景、进行中工作项清单（标题/状态/DDL）、
成员负载。建议工作项拆分、协作点与 DDL；潜在风险放 risks。
"""

SYSTEM_PROMPT = (
    "你是规划助手（Planning Advisor），把需求拆分为可执行的工作项并给出协作点与"
    "排期建议。只输出一个 JSON 对象，不要输出任何其他文字、解释或 Markdown 代码块标记。"
    "JSON 结构："
    '{"content": {"summary": "一句话结论", "rationale": "拆分依据", '
    '"work_item_breakdown": [{"title": "工作项标题", "description": "做什么", '
    '"suggested_due_date": "YYYY-MM-DD 或 null", "collaborator_hint": "需要哪类成员协作或 null"}], '
    '"collaboration_points": ["协作点1"]}, '
    '"confidence": 0.0到1.0之间的数字, "risks": "潜在风险和限制"}。'
    "拆分要考虑进行中工作项的既有负载，避免排期冲突；"
    "suggested_due_date 只是建议，不会自动写入正式工作项；"
    "collaboration_points 指出工作项之间或与既有工作项的协作依赖；信息不足时在 risks 中说明。"
)


def render_user_prompt(
    *,
    project_name: str,
    requirement: str,
    work_item_title: str | None,
    open_work_items: list[dict],
    workload: list[dict],
) -> str:
    """组装 user 提示词（最小上下文：需求背景 + 进行中工作项 + 负载）。"""
    import json

    lines = [f"项目：{project_name or '（未知）'}"]
    if work_item_title:
        lines.append(f"关联工作项：{work_item_title}")
    lines += [
        "",
        "需求背景：",
        requirement.strip() or "（空）",
        "",
        "进行中工作项（标题 / 状态 / 截止时间）：",
        json.dumps(open_work_items, ensure_ascii=False, indent=2),
        "",
        "成员当前负载（活跃工作项数）：",
        json.dumps(workload, ensure_ascii=False, indent=2),
    ]
    return "\n".join(lines)
