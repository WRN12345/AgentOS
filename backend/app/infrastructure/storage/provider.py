"""存储抽象，业务层只依赖 `StorageProvider` 接口。

数据库仅保存相对 `storage_key`，不保存宿主机绝对路径。上传通过 `stage` 流式写入，
再由 `commit` 原子落位或由 `discard` 补偿清理；下载通过 `iter_chunks` 流式读取。
上传目录不得直接暴露给静态服务或反向代理。
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import PurePosixPath, PureWindowsPath

from app.core.config import settings

DEFAULT_CHUNK_SIZE = 64 * 1024


def validate_key(storage_key: str) -> PurePosixPath:
    key = PurePosixPath(storage_key)
    if (
        not storage_key
        or "\x00" in storage_key
        or key.is_absolute()
        or PureWindowsPath(storage_key).is_absolute()
        or "\\" in storage_key
        or any(part in ("", ".", "..") for part in storage_key.split("/"))
    ):
        raise ValueError(f"Invalid storage_key: {storage_key!r}")
    return key


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

    @asynccontextmanager
    async def open(self, storage_key: str, mode: str = "rb"):
        """打开顺序二进制流；写入成功时在退出上下文时发布。"""
        if mode not in ("rb", "wb"):
            raise ValueError("Storage mode must be 'rb' or 'wb'")
        validate_key(storage_key)
        if mode == "rb":
            async with self._open_reader(storage_key) as reader:
                yield reader
        else:
            staged = await self.stage()
            try:
                yield staged
                await self.commit(staged, storage_key)
            finally:
                await self.discard(staged)

    def _open_reader(self, storage_key: str):
        raise NotImplementedError

    async def save(self, storage_key: str, data: bytes) -> None:
        """一次性写入小文件（原子落位）。"""
        async with self.open(storage_key, "wb") as stream:
            await stream.write(data)

    async def load(self, storage_key: str) -> bytes:
        """读取完整内容。"""
        async with self.open(storage_key, "rb") as stream:
            return await stream.read()

    @abstractmethod
    async def delete(self, storage_key: str) -> None:
        """删除文件；不存在时静默成功（补偿清理场景）。"""

    @abstractmethod
    async def exists(self, storage_key: str) -> bool:
        """存在性检查。"""

    async def iter_chunks(
        self, storage_key: str, chunk_size: int = DEFAULT_CHUNK_SIZE
    ) -> AsyncIterator[bytes]:
        """流式读取（下载用），避免整文件载入内存。"""
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        async with self.open(storage_key, "rb") as stream:
            while chunk := await stream.read(chunk_size):
                yield chunk

    @abstractmethod
    async def stage(self) -> StagedUpload:
        """开启一次本地磁盘暂存写入，由 commit 发布完整对象。"""

    @abstractmethod
    async def commit(self, staged: StagedUpload, storage_key: str) -> None:
        """把暂存内容原子落位到 storage_key（本地实现用 os.replace）。"""

    @abstractmethod
    async def discard(self, staged: StagedUpload) -> None:
        """在校验或落库失败时丢弃暂存内容。"""


_provider: StorageProvider | None = None
_providers: dict[str, StorageProvider] = {}


def storage_for(backend: str) -> StorageProvider:
    """根据持久化的后端名称返回缓存的 Provider。"""
    global _provider
    if backend == "local":
        # 测试替换 storage_root 时，允许重置本地单例。
        if _provider is None:
            from app.infrastructure.storage.local import LocalStorageProvider

            _provider = LocalStorageProvider(settings.storage_root)
        return _provider
    if backend == "minio":
        settings.validate_minio()
        if backend not in _providers:
            from app.infrastructure.storage.minio import MinioStorageProvider

            _providers[backend] = MinioStorageProvider(
                settings.minio_endpoint,
                settings.minio_access_key,
                settings.minio_secret_key,
                settings.minio_bucket,
                settings.minio_secure,
            )
        return _providers[backend]
    raise ValueError(f"Unsupported storage backend: {backend}")


def get_storage_provider() -> StorageProvider:
    """Provider 单例工厂，同时作为 FastAPI 依赖项。

    业务层经 Depends(get_storage_provider) 注入；测试用
    app.dependency_overrides 覆盖注入任意 StorageProvider 实现。
    """
    return storage_for(settings.storage_backend)
