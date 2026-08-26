"""文档内容提取器（设计文档第 4 节）：文件字节 → 纯文本，供索引管道切块。

格式支持矩阵（与第 4 节一致）：
- .md / .txt：直接读（utf-8，解码失败降级 replace，不因编码问题阻塞索引）；
- .pdf：pypdf 提取文字层；提取结果为空视为扫描件（本质是图片，本期后置）→ 不支持；
- .docx：python-docx 提取段落与表格文字；
- 其余（zip/图片等）：不支持读取内容。

两种失败语义对应索引状态机的不同终态（第 6 节）：
- UnsupportedFormatError → unindexed（格式不支持，终态，不重试）；
- ExtractionFailedError → failed（文件损坏等，可重试）。
"""

import io

from pypdf import PdfReader
from pypdf.errors import PdfReadError

#: 支持读取内容的扩展名（其余一律 UnsupportedFormatError）
SUPPORTED_EXTENSIONS = frozenset({".md", ".txt", ".pdf", ".docx"})


class UnsupportedFormatError(Exception):
    """格式不支持读取内容（zip/图片/扫描件 PDF 等）→ 索引状态 unindexed。"""


class ExtractionFailedError(Exception):
    """声称支持但提取失败（文件损坏等）→ 索引状态 failed（可重试）。"""


def _extract_text_file(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        # 非 utf-8（如 GBK 文本）：降级 replace，保留可检索的大部分内容
        return data.decode("utf-8", errors="replace")


def _extract_pdf(data: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [(page.extract_text() or "") for page in reader.pages]
    except (PdfReadError, ValueError, OSError) as exc:
        raise ExtractionFailedError(f"PDF 解析失败: {exc}") from exc
    text = "\n\n".join(p for p in pages if p.strip())
    if not text.strip():
        # 提取不到文字层 → 扫描件（设计文档第 4 节：本质是图片，本期后置）
        raise UnsupportedFormatError("扫描件 PDF（无文字层）")
    return text


def _extract_docx(data: bytes) -> str:
    from docx import Document

    try:
        document = Document(io.BytesIO(data))
    except Exception as exc:  # python-docx 对损坏文件抛多种异常（PackageNotFoundError 等）
        raise ExtractionFailedError(f"DOCX 解析失败: {exc}") from exc
    parts = [p.text for p in document.paragraphs if p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            parts.extend(cell.text for cell in row.cells if cell.text.strip())
    return "\n\n".join(parts)


def extract_text(filename: str, data: bytes) -> str:
    """按扩展名提取纯文本。

    - 不支持的格式抛 UnsupportedFormatError（→ unindexed）；
    - 支持但解析失败抛 ExtractionFailedError（→ failed，可重试）；
    - 提取结果可能是空字符串（如空文档），由索引管道按"空内容"处理。
    """
    suffix = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    if suffix not in SUPPORTED_EXTENSIONS:
        raise UnsupportedFormatError(f"不支持读取内容的格式: {suffix or '<无扩展名>'}")
    if suffix in (".md", ".txt"):
        return _extract_text_file(data)
    if suffix == ".pdf":
        return _extract_pdf(data)
    return _extract_docx(data)
