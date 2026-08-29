"""文本切块器的长度、边界和重叠行为测试。

- 每块不超过 max_chars；空文本返回空；
- 段落能整块放入时不拆；段落超长按句末标点拆；单句超长才硬切；
- 相邻块存在重叠，跨块语义至少完整出现一次。
"""

from app.domains.memory.chunking import chunk_text


def test_empty_text_returns_empty() -> None:
    assert chunk_text("") == []
    assert chunk_text("   \n\n  ") == []


def test_short_text_single_chunk() -> None:
    text = "这是一段很短的文档。"
    assert chunk_text(text) == [text]


def test_paragraphs_kept_intact_when_possible() -> None:
    """多个短段落应贪心合并且保持段落完整。"""
    paragraphs = [f"第{i}段内容。" * 5 for i in range(6)]  # 每段 30 字
    text = "\n\n".join(paragraphs)
    chunks = chunk_text(text, max_chars=100, overlap_chars=0)

    assert len(chunks) == 2  # 每块三段（30*3+2 换行 ≤ 100）
    for chunk in chunks:
        for part in chunk.split("\n"):
            assert part in paragraphs


def test_long_paragraph_split_at_sentence_boundary() -> None:
    """超长段落按句末标点拆，句子不被切断。"""
    sentences = [f"这是第{i}句话，用来凑够长度。" for i in range(20)]
    text = "".join(sentences)
    chunks = chunk_text(text, max_chars=60, overlap_chars=0)

    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 60
        assert chunk.endswith("。")


def test_single_long_sentence_hard_cut() -> None:
    """单句超过 max_chars 时硬切（最后手段），块长仍不超上限。"""
    text = "无标点长句" * 100  # 500 字无句末标点
    chunks = chunk_text(text, max_chars=60, overlap_chars=0)

    assert all(len(c) <= 60 for c in chunks)
    assert "".join(chunks) == text


def test_adjacent_chunks_overlap() -> None:
    """相邻块有重叠：前一块末尾内容出现在后一块开头。"""
    text = "\n\n".join([f"段落{i}，内容若干。" * 8 for i in range(10)])
    chunks = chunk_text(text, max_chars=100, overlap_chars=20)

    assert len(chunks) >= 2
    for prev, nxt in zip(chunks, chunks[1:]):
        tail = prev[-20:]
        assert nxt.startswith(tail[:10])  # 尾部片段在后一块开头出现


def test_chunk_never_exceeds_max_even_with_overlap() -> None:
    text = "\n\n".join(["段" * 90 for _ in range(10)])
    chunks = chunk_text(text, max_chars=100, overlap_chars=50)
    assert all(len(c) <= 100 for c in chunks)
