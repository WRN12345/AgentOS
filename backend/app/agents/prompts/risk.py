"""Workflow Risk Agent 提示词模板。

仅向模型提供识别风险所需的逾期、临期或阻塞工作项、近期转派记录和等待中
协作请求，不提供无关资料或敏感正文。输入均来自系统侧只读查询，输出平铺的
risks 列表。
"""

SYSTEM_PROMPT = (
    "你是工作流风险分析助手（Workflow Risk Agent），识别项目中的逾期、阻塞、"
    "频繁转派和协作等待风险。只输出一个 JSON 对象，不要输出任何其他文字、"
    "解释或 Markdown 代码块标记。"
    "JSON 结构："
    '{"content": {"summary": "一句话结论", "rationale": "整体判断依据", '
    '"risks": [{"type": "overdue|blocked|frequent_transfer|collaboration_wait", '
    '"target_type": "work_item|collaboration_request", "target_id": "对象 ID", '
    '"title": "对象标题", "severity": "high|medium|low", "detail": "风险说明"}]}, '
    '"confidence": 0.0到1.0之间的数字, "risks": "本次分析的局限和需要人工确认的点"}。'
    "type 只能取这四类：overdue（逾期或临期）、blocked（阻塞）、"
    "frequent_transfer（频繁转派）、collaboration_wait（协作等待）；"
    "target_id 必须取自输入数据中的真实 ID，不得编造；"
    "没有发现风险时 risks 返回空列表，并在 summary 中说明；"
    "你只生成建议，不能改变任何业务状态。"
)


def render_user_prompt(
    *,
    project_name: str,
    now: str,
    overdue_items: list[dict],
    due_soon_items: list[dict],
    blocked_items: list[dict],
    transfer_history: list[dict],
    waiting_collaborations: list[dict],
) -> str:
    """使用风险相关的只读查询结果组装最小 user 提示词。"""
    import json

    lines = [
        f"项目：{project_name or '（未知）'}",
        f"当前时间：{now}",
        "",
        "已逾期工作项（标题 / 状态 / 截止时间 / 优先级）：",
        json.dumps(overdue_items, ensure_ascii=False, indent=2),
        "",
        "临期工作项（截止时间在临近窗口内）：",
        json.dumps(due_soon_items, ensure_ascii=False, indent=2),
        "",
        "阻塞中工作项（含阻塞起始时间）：",
        json.dumps(blocked_items, ensure_ascii=False, indent=2),
        "",
        "近期转派申请记录（用于识别频繁转派）：",
        json.dumps(transfer_history, ensure_ascii=False, indent=2),
        "",
        "等待中的协作请求（未终态，用于识别协作等待）：",
        json.dumps(waiting_collaborations, ensure_ascii=False, indent=2),
    ]
    return "\n".join(lines)
