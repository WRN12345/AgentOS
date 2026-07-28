"""Summary Agent 提示词模板（10.1 节，T5.5）。

模型只接收最小上下文（16 节）：工作项状态统计、近期完成事项、待审批事项、
风险输入（逾期/阻塞计数与清单），均为只读工具的系统侧真实查询结果。
输出项目进展 / 已完成事项 / 待审批 / 风险摘要，支撑日报与负责人汇总（4.2 节）。
"""

SYSTEM_PROMPT = (
    "你是项目摘要助手（Summary Agent），根据系统提供的真实统计数据生成项目"
    "进展摘要，支撑日报、阶段总结和负责人汇总。只输出一个 JSON 对象，"
    "不要输出任何其他文字、解释或 Markdown 代码块标记。"
    "JSON 结构："
    '{"content": {"summary": "一句话总体进展", "rationale": "摘要依据", '
    '"progress": "项目进展概述（基于状态统计）", '
    '"completed": ["已完成事项1"], '
    '"pending_approvals": ["待审批事项1"], '
    '"risks": ["风险提示1"]}, '
    '"confidence": 0.0到1.0之间的数字, "risks": "本摘要的局限和需要人工核实的点"}。'
    "completed / pending_approvals / risks 必须严格基于输入数据，不得编造"
    "输入中不存在的事项或数字；数据为空时对应列表返回空并在 summary 中说明；"
    "你只生成摘要建议，不能改变任何业务状态。"
)


def render_user_prompt(
    *,
    project_name: str,
    now: str,
    status_counts: dict[str, int],
    completed_items: list[dict],
    pending_approvals: dict[str, list[dict]],
    overdue_items: list[dict],
    blocked_items: list[dict],
) -> str:
    """组装 user 提示词（最小上下文：真实统计 + 完成/待审批/风险清单）。"""
    import json

    lines = [
        f"项目：{project_name or '（未知）'}",
        f"当前时间：{now}",
        "",
        "工作项状态统计（各状态数量）：",
        json.dumps(status_counts, ensure_ascii=False, indent=2),
        "",
        "近期完成的工作项：",
        json.dumps(completed_items, ensure_ascii=False, indent=2),
        "",
        "待审批事项（待审工作项 / 待批转派 / 待批 DDL 变更）：",
        json.dumps(pending_approvals, ensure_ascii=False, indent=2),
        "",
        "风险输入——已逾期工作项：",
        json.dumps(overdue_items, ensure_ascii=False, indent=2),
        "",
        "风险输入——阻塞中工作项：",
        json.dumps(blocked_items, ensure_ascii=False, indent=2),
    ]
    return "\n".join(lines)
