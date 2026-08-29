"""尽量保留段落和句子边界的文本切块器。

先按空行拆分段落；超长段落再按句末标点或换行拆分；单句仍超长时才在
`max_chars` 处硬切。

新块复用前一块末尾 `overlap_chars` 个字符，减少跨块边界的语义损失。
"""

import re

#: 默认块大小与相邻块重叠量
DEFAULT_MAX_CHARS = 500
DEFAULT_OVERLAP_CHARS = 50

# 在中英文句末标点后或换行处切分
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
    """切分文本，每块不超过 `max_chars`，相邻块约重叠 `overlap_chars`。

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
            # 重叠后超限时放弃本次重叠，仍保证块大小上限
            current = segment
    if current:
        chunks.append(current)
    return chunks
