"""本地文件系统存储实现。

文件写入配置的本地根目录，`storage_key` 是根目录内的相对路径。暂存文件位于
`.tmp/` 子目录，与正式目录处于同一文件系统，保证 `os.replace` 原子生效。

本地磁盘小文件读写为亚毫秒级同步操作，直接在异步方法内执行；
`iter_chunks` 使用 `asyncio.to_thread`，避免下载大文件时阻塞事件循环。
"""

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path, PurePosixPath

from app.infrastructure.storage.provider import DEFAULT_CHUNK_SIZE, StagedUpload, StorageProvider


def _validate_key(storage_key: str) -> PurePosixPath:
    """拒绝绝对路径与路径穿越（".."），storage_key 必须是相对键。"""
    key = PurePosixPath(storage_key)
    if key.is_absolute() or ".." in key.parts or not storage_key:
        raise ValueError(f"非法 storage_key: {storage_key!r}")
    return key


class _LocalStagedUpload(StagedUpload):
    def __init__(self, path: Path) -> None:
        self.path = path
        self._file = path.open("wb")

    async def write(self, chunk: bytes) -> None:
        await asyncio.to_thread(self._file.write, chunk)

    def close(self) -> None:
        if not self._file.closed:
            self._file.close()


class LocalStorageProvider(StorageProvider):
    backend_name = "local"

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._tmp_dir = self._root / ".tmp"

    def _resolve(self, storage_key: str) -> Path:
        return self._root / _validate_key(storage_key)

    async def save(self, storage_key: str, data: bytes) -> None:
        staged = await self.stage()
        try:
            await staged.write(data)
            await self.commit(staged, storage_key)
        except BaseException:
            await self.discard(staged)
            raise

    async def load(self, storage_key: str) -> bytes:
        return await asyncio.to_thread(self._resolve(storage_key).read_bytes)

    async def delete(self, storage_key: str) -> None:
        await asyncio.to_thread(self._resolve(storage_key).unlink, True)

    async def exists(self, storage_key: str) -> bool:
        return await asyncio.to_thread(self._resolve(storage_key).is_file)

    async def iter_chunks(
        self, storage_key: str, chunk_size: int = DEFAULT_CHUNK_SIZE
    ) -> AsyncIterator[bytes]:
        path = self._resolve(storage_key)
        f = await asyncio.to_thread(path.open, "rb")
        try:
            while chunk := await asyncio.to_thread(f.read, chunk_size):
                yield chunk
        finally:
            await asyncio.to_thread(f.close)

    async def stage(self) -> StagedUpload:
        await asyncio.to_thread(self._tmp_dir.mkdir, parents=True, exist_ok=True)
        return _LocalStagedUpload(self._tmp_dir / uuid.uuid4().hex)

    async def commit(self, staged: StagedUpload, storage_key: str) -> None:
        assert isinstance(staged, _LocalStagedUpload)
        staged.close()
        target = self._resolve(storage_key)
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        # 暂存目录与目标目录位于同一文件系统，因此替换操作具有原子性。
        await asyncio.to_thread(os.replace, staged.path, target)

    async def discard(self, staged: StagedUpload) -> None:
        assert isinstance(staged, _LocalStagedUpload)
        staged.close()
        await asyncio.to_thread(staged.path.unlink, True)
