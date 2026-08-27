"""交付物接口请求/响应模型（7.5、12.5 节）。

- git_link / text 类型必须携带 content；file 类型必须携带 file_id（引用已上传文件）；
- file 类型响应内嵌文件摘要（含 sha256），可追溯 stored_files 哈希记录（2.1 节）。
"""

import re
import uuid
from datetime import datetime
from typing import Literal
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, Field, model_validator

from app.domains.work_items.schemas import MemberBrief


_PULL_REQUEST_PATHS = {
    "github.com": re.compile(r"^/([^/]+)/([^/]+)/pull/([1-9][0-9]*)/?$"),
    "gitee.com": re.compile(r"^/([^/]+)/([^/]+)/pulls/([1-9][0-9]*)/?$"),
}
_GITLAB_MERGE_REQUEST_PATH = re.compile(
    r"^/(.+)/-/merge_requests/([1-9][0-9]*)/?$"
)
_STANDARD_COMMIT_PATH = re.compile(
    r"^/([^/]+)/([^/]+)/commit/([0-9a-fA-F]{7,40})/?$"
)
_GITLAB_COMMIT_PATH = re.compile(r"^/(.+)/-/commit/([0-9a-fA-F]{7,40})/?$")
_INVALID_PERCENT_ESCAPE = re.compile(r"%(?![0-9a-fA-F]{2})")


def _normalize_git_delivery_url(value: str) -> str:
    """校验受支持的 Git 交付 URL，并返回规范地址。"""
    candidate = value.strip()
    # 拒绝任何 ASCII 控制字符（含 NUL、制表符、换行）：
    if any(ord(ch) <= 0x1F or ord(ch) == 0x7F for ch in candidate):
        raise ValueError("请输入受支持的 PR、MR 或 Commit 链接")
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("请输入受支持的 PR、MR 或 Commit 链接") from exc

    if (
        parsed.scheme.lower() != "https"
        or parsed.hostname is None
        # 原始 netloc 必须精确等于主机名：拒绝空端口（github.com:）、用户信息、显式端口
        or parsed.netloc.lower() != parsed.hostname.lower()
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        # 拒绝任何 ? 或 #（含空查询/锚点 .../pull/42?、.../pull/42#），与前端同一公开规则
        or "?" in candidate
        or "#" in candidate
    ):
        raise ValueError("请输入受支持的 PR、MR 或 Commit 链接")

    path_segments = parsed.path.removeprefix("/").split("/")
    if path_segments and path_segments[-1] == "":
        path_segments.pop()
    if (
        not parsed.path.startswith("/")
        or not path_segments
        or any(not segment for segment in path_segments)
        or _INVALID_PERCENT_ESCAPE.search(parsed.path)
    ):
        raise ValueError("请输入受支持的 PR、MR 或 Commit 链接")
    try:
        decoded_segments = [
            unquote(segment, errors="strict") for segment in path_segments
        ]
    except UnicodeError as exc:
        raise ValueError("请输入受支持的 PR、MR 或 Commit 链接") from exc
    if any(
        segment in {".", ".."} or "/" in segment or "\\" in segment
        for segment in decoded_segments
    ):
        raise ValueError("请输入受支持的 PR、MR 或 Commit 链接")

    hostname = parsed.hostname.lower()
    if hostname in _PULL_REQUEST_PATHS:
        commit_match = _STANDARD_COMMIT_PATH.fullmatch(parsed.path)
        if commit_match is not None:
            owner, repository, commit_sha = commit_match.groups()
            return f"https://{hostname}/{owner}/{repository}/commit/{commit_sha.lower()}"

    pattern = _PULL_REQUEST_PATHS.get(hostname)
    match = pattern.fullmatch(parsed.path) if pattern is not None else None
    if hostname == "gitlab.com":
        commit_match = _GITLAB_COMMIT_PATH.fullmatch(parsed.path)
        if commit_match is not None:
            repository_path, commit_sha = commit_match.groups()
            if len(repository_path.split("/")) >= 2:
                return f"https://gitlab.com/{repository_path}/-/commit/{commit_sha.lower()}"
        gitlab_match = _GITLAB_MERGE_REQUEST_PATH.fullmatch(parsed.path)
        if gitlab_match is not None:
            repository_path, merge_request_number = gitlab_match.groups()
            if len(repository_path.split("/")) >= 2:
                return (
                    f"https://gitlab.com/{repository_path}/-/merge_requests/"
                    f"{merge_request_number}"
                )
    if match is None:
        raise ValueError("请输入受支持的 PR、MR 或 Commit 链接")
    owner, repository, pull_number = match.groups()
    review_segment = "pull" if hostname == "github.com" else "pulls"
    return f"https://{hostname}/{owner}/{repository}/{review_segment}/{pull_number}"


class DeliverableCreateIn(BaseModel):
    """提交新版本交付物：版本号由服务端按工作项递增（7.5 节），客户端不传。"""

    type: Literal["git_link", "text", "file"]
    content: str | None = Field(default=None, min_length=1)
    file_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _check_type_payload(self) -> "DeliverableCreateIn":
        if self.type in ("git_link", "text"):
            if not self.content:
                raise ValueError("git_link/text 类型必须携带 content")
            if self.file_id is not None:
                raise ValueError("git_link/text 类型不应携带 file_id")
            if self.type == "git_link":
                self.content = _normalize_git_delivery_url(self.content)
        else:  # file
            if self.file_id is None:
                raise ValueError("file 类型必须携带 file_id")
            if self.content:
                raise ValueError("file 类型不应携带 content")
        return self


class FileBrief(BaseModel):
    """file 类型交付物引用的文件摘要：sha256 供完整性追溯。"""

    id: uuid.UUID
    original_filename: str
    size_bytes: int
    mime_type: str
    sha256: str


class DeliverableOut(BaseModel):
    id: uuid.UUID
    work_item_id: uuid.UUID
    type: str
    content: str | None
    file: FileBrief | None
    version: int
    submitted_by: MemberBrief
    created_at: datetime
    updated_at: datetime


class DeliverableReviewBrief(BaseModel):
    """交付物的审核结论摘要（反馈正文仅负责人与主执行人可见，16 节）。"""

    decision: str
    feedback: str | None
    reviewed_by: MemberBrief
    created_at: datetime


class DeliverableListItemOut(BaseModel):
    """交付物列表项：交付物聚合页与"我的申请"（role=mine）共用。

    review 为 null 表示尚未审核；feedback 按 16 节仅对负责人与提交人返回。
    """

    id: uuid.UUID
    work_item_id: uuid.UUID
    work_item_title: str
    type: str
    version: int
    submitted_by: MemberBrief
    created_at: datetime
    review: DeliverableReviewBrief | None
