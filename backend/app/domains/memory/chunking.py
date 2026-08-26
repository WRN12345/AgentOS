"""结构感知切块器（设计文档 15.2）：每块约 500 字、相邻块略有重叠、不切断句子段落。

切分优先级（尽量保留语义边界）：
1. 按空行分段落，段落能整块放入就不拆；
2. 段落超长时按句末标点（。！？；.!?; 与换行）拆句；
3. 单句仍超长时才在 max_chars 处硬切（无法避免的最后手段）。

重叠实现：新块以前一块末尾 overlap_chars 个字符开头，保证跨块边界的
语义在两块中至少完整出现一次。
"""

import re

#: 默认块大小与重叠量（15.2，按实际文档效果可调）
DEFAULT_MAX_CHARS = 500
DEFAULT_OVERLAP_CHARS = 50

# 句末边界：中英文句末标点之后、或单个换行
_SENTENCE_BOUNDARY = re.compile(r"(?<=[。！？；.!?;])|\n")
_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")


def _split_paragraph(paragraph: str, max_chars: int) -> list[str]:
    """把超长段落拆成句子级片段；单句超长时硬切。"""
    parts: list[str] = []
    buf = ""
    for piece in _SENTENCE_BOUNDARY.split(paragraph):
        piece = piece.strip()
        if not piece:
            continue
        if len(buf) + len(piece) <= max_chars:
            buf += piece
            continue
        if buf:
            parts.append(buf)
        buf = piece
        while len(buf) > max_chars:
            parts.append(buf[:max_chars])
            buf = buf[max_chars:]
    if buf:
        parts.append(buf)
    return parts


def _segments(text: str, max_chars: int) -> list[str]:
    segments: list[str] = []
    for paragraph in _PARAGRAPH_SPLIT.split(text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) <= max_chars:
            segments.append(paragraph)
        else:
            segments.extend(_split_paragraph(paragraph, max_chars))
    return segments


def chunk_text(
    text: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> list[str]:
    """把文本切成若干块，每块不超过 max_chars，相邻块重叠约 overlap_chars。

    空文本/纯空白返回空列表。
    """
    if not text or not text.strip():
        return []

    chunks: list[str] = []
    current = ""
    for segment in _segments(text.strip(), max_chars):
        candidate = f"{current}\n{segment}" if current else segment
        if len(candidate) <= max_chars:
            current = candidate
            continue
        chunks.append(current)
        tail = current[-overlap_chars:] if overlap_chars > 0 else ""
        current = f"{tail}\n{segment}" if tail else segment
        if len(current) > max_chars:
            # 极端情况：重叠尾部 + 整段仍超长，放弃本次重叠（段落本身已达上限）
            current = segment
    if current:
        chunks.append(current)
    return chunks
