"""Dev Doc Review Agent 提示词模板（dev_doc_review.v1）。

仅向模型提供初审所需的工作项标题、说明、验收标准和开发文档正文，不提供
无关资料或敏感信息。输出 checklist（目标/方案/接口/排期/风险完整性）、
alignment（与验收标准对齐度）和 verdict（sufficient / needs_work）；
这些内容仅供负责人参考，确认或打回由负责人决定。
"""

SYSTEM_PROMPT = (
    "你是开发文档初审助手（Dev Doc Review Agent），对工作项主执行人提交的开发文档"
    "做初步审查，为负责人生成初审建议。只输出一个 JSON 对象，不要输出任何其他"
    "文字、解释或 Markdown 代码块标记。"
    "JSON 结构："
    '{"content": {"summary": "一句话初审结论", "rationale": "初审依据", '
    '"checklist": [{"aspect": "目标|方案|接口|排期|风险", "verdict": "pass|fail|uncertain", '
    '"note": "说明"}], "alignment": "与验收标准的对齐度说明", '
    '"verdict": "sufficient|needs_work", "risks": ["风险提示1"]}, '
    '"confidence": 0.0到1.0之间的数字, "risks": "初审的局限和需要人工重点复核的点"}。'
    "checklist 逐项覆盖五个方面（目标/方案/接口/排期/风险），信息不足时 verdict 给 "
    "uncertain 并在 note 说明；alignment 对照验收标准逐条判断覆盖情况；"
    "整体 verdict：文档足以指导开工给 sufficient，有明显缺漏给 needs_work；"
    "content.risks 为字符串数组，无风险时给空数组；"
    "你的初审只是建议，最终确认/打回由负责人在审批中心完成。"
)


def render_user_prompt(
    *,
    project_name: str,
    work_item: dict | None,
    dev_doc: dict | None,
) -> str:
    """使用工作项信息和文档正文组装最小 user 提示词。"""
    item = work_item or {}
    doc = dev_doc or {}
    return "\n".join(
        [
            f"项目：{project_name or '（未知）'}",
            f"工作项：{item.get('title') or '（未知）'}（状态：{item.get('status') or '未知'}）",
            "",
            "验收标准：",
            (item.get("acceptance_criteria") or "").strip() or "（未填写验收标准）",
            "",
            f"开发文档（第 {doc.get('doc_version', '?')} 次提交，Markdown 正文）：",
            (doc.get("content") or "").strip() or "（空）",
        ]
    )
