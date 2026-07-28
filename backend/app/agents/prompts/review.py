"""Deliverable Review Agent 提示词模板（10.1 节，T5.5）。

最小上下文（16 节）：工作项验收标准 + 最新交付物版本信息——文本交付物正文、
文件元数据（文件名/大小/类型/哈希，不读文件原文）、Git 链接文本。
输出负责人审核清单（checklist），只生成建议，不写 reviews 表（10.3 节）。
"""

SYSTEM_PROMPT = (
    "你是交付物初审助手（Deliverable Review Agent），根据工作项验收标准对最新"
    "交付物做初步审查，为负责人生成审核清单。只输出一个 JSON 对象，不要输出"
    "任何其他文字、解释或 Markdown 代码块标记。"
    "JSON 结构："
    '{"content": {"summary": "一句话初审结论", "rationale": "初审依据", '
    '"checklist": [{"checkpoint": "检查点", "verdict": "pass|fail|uncertain", '
    '"evidence": "得出结论的依据（引用交付物内容或元数据）"}]}, '
    '"confidence": 0.0到1.0之间的数字, "risks": "初审的局限和需要人工重点复核的点"}。'
    "checklist 逐项覆盖验收标准；verdict=uncertain 表示凭现有信息无法判断，"
    "需人工核实；文件类交付物只有元数据（文件名/大小/类型/哈希），"
    "不要假装读过文件内容，对文件内容的判断一律 uncertain；"
    "你的清单只是建议，最终审核由负责人在正式审核流程中完成。"
)


def render_user_prompt(
    *,
    project_name: str,
    work_item: dict | None,
    acceptance_criteria: str | None,
    latest_deliverable: dict | None,
) -> str:
    """组装 user 提示词（最小上下文：验收标准 + 最新交付物版本信息）。"""
    import json

    item = work_item or {}
    lines = [
        f"项目：{project_name or '（未知）'}",
        f"工作项：{item.get('title') or '（未知）'}（状态：{item.get('status') or '未知'}）",
        "",
        "验收标准：",
        (acceptance_criteria or "").strip() or "（未填写验收标准）",
        "",
        "最新交付物版本信息（文本正文 / Git 链接文本 / 文件元数据，不含文件原文）：",
        json.dumps(latest_deliverable, ensure_ascii=False, indent=2)
        if latest_deliverable
        else "（无交付物）",
    ]
    return "\n".join(lines)
