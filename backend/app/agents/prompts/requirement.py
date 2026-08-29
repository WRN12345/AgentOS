"""Requirement Analyst 提示词模板。

仅向模型提供整理需求所需的项目名、可选关联工作项标题和需求原文，不提供
无关资料或敏感信息。模型只输出 JSON；suggestion_type、prompt_version 和
fact_refs 由系统侧注入，模型只需产出 content、confidence 和 risks。
"""

SYSTEM_PROMPT = (
    "你是需求分析助手（Requirement Analyst），负责把自然语言需求整理为结构化内容。"
    "只输出一个 JSON 对象，不要输出任何其他文字、解释或 Markdown 代码块标记。"
    "JSON 结构："
    '{"content": {"summary": "一句话结论", "rationale": "整理依据", '
    '"goals": ["目标1", "目标2"], "constraints": ["约束1"], '
    '"deliverables": ["交付物1"], "acceptance_criteria": ["验收标准1"]}, '
    '"confidence": 0.0到1.0之间的数字, "risks": "风险和限制"}。'
    "goals/constraints/deliverables/acceptance_criteria 均为字符串数组，不得为空数组以外的非数组类型；"
    "信息不足时在 risks 中说明，不要编造。"
)


def render_user_prompt(
    *, project_name: str, work_item_title: str | None, requirement: str
) -> str:
    """使用项目名、可选工作项标题和需求原文组装最小 user 提示词。"""
    lines = [f"项目：{project_name or '（未知）'}"]
    if work_item_title:
        lines.append(f"关联工作项：{work_item_title}")
    lines += ["", "需求原文：", requirement.strip() or "（空）"]
    return "\n".join(lines)
