"""存储抽象，业务层只依赖 `StorageProvider` 接口。

数据库仅保存相对 `storage_key`，不保存宿主机绝对路径。上传通过 `stage` 流式写入，
再由 `commit` 原子落位或由 `discard` 补偿清理；下载通过 `iter_chunks` 流式读取。
上传目录不得直接暴露给静态服务或反向代理。
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from app.core.config import settings

DEFAULT_CHUNK_SIZE = 64 * 1024


class StagedUpload(ABC):
    """上传暂存写入器：流式接收分块内容，由 Provider 负责其生命周期。"""

    @abstractmethod
    async def write(self, chunk: bytes) -> None:
        """追加一块内容。"""


class StorageProvider(ABC):
    """存储后端最小接口：写入、读取、删除、存在性检查。

    方法签名面向 bytes 或字节流；storage_key 为后端内相对键，
    禁止包含绝对路径或 ".."（实现方必须拒绝并抛 ValueError）。
    """

    backend_name: str

    @abstractmethod
    async def save(self, storage_key: str, data: bytes) -> None:
        """一次性写入小文件（原子落位）。"""

    @abstractmethod
    async def load(self, storage_key: str) -> bytes:
        """读取完整内容。"""

    @abstractmethod
    async def delete(self, storage_key: str) -> None:
        """删除文件；不存在时静默成功（补偿清理场景）。"""

    @abstractmethod
    async def exists(self, storage_key: str) -> bool:
        """存在性检查。"""

    @abstractmethod
    def iter_chunks(
        self, storage_key: str, chunk_size: int = DEFAULT_CHUNK_SIZE
    ) -> AsyncIterator[bytes]:
        """流式读取（下载用），避免整文件载入内存。"""

    @abstractmethod
    async def stage(self) -> StagedUpload:
        """开启一次暂存写入（与正式存储同后端，保证 commit 可原子完成）。"""

    @abstractmethod
    async def commit(self, staged: StagedUpload, storage_key: str) -> None:
        """把暂存内容原子落位到 storage_key（本地实现用 os.replace）。"""

    @abstractmethod
    async def discard(self, staged: StagedUpload) -> None:
        """在校验或落库失败时丢弃暂存内容。"""


_provider: StorageProvider | None = None


def get_storage_provider() -> StorageProvider:
    """Provider 单例工厂，同时作为 FastAPI 依赖项。

    业务层经 Depends(get_storage_provider) 注入；测试用
    app.dependency_overrides 覆盖注入任意 StorageProvider 实现。
    """
    global _provider
    if _provider is None:
        if settings.storage_backend == "local":
            from app.infrastructure.storage.local import LocalStorageProvider

            _provider = LocalStorageProvider(settings.storage_root)
        else:
            raise RuntimeError(f"不支持的存储后端: {settings.storage_backend}")
    return _provider
