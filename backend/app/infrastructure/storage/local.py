"""Local storage with disk staging and atomic publication."""

import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from app.infrastructure.storage.provider import StagedUpload, StorageProvider, validate_key
from app.infrastructure.storage.streams import ReadStream, StagedFile, run_sync


class LocalStorageProvider(StorageProvider):
    backend_name = "local"

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).resolve()

    def _resolve(self, storage_key: str) -> Path:
        path = self._root / validate_key(storage_key)
        if not path.resolve().is_relative_to(self._root):
            raise ValueError(f"Storage key escapes storage root: {storage_key!r}")
        return path

    @asynccontextmanager
    async def _open_reader(self, storage_key: str):
        stream = await run_sync(self._resolve(storage_key).open, "rb", on_cancel=lambda f: f.close())
        reader = ReadStream(stream)
        try:
            yield reader
        finally:
            reader.closed = True
            await run_sync(stream.close)

    async def delete(self, storage_key: str) -> None:
        await run_sync(self._resolve(storage_key).unlink, missing_ok=True)

    async def exists(self, storage_key: str) -> bool:
        return await run_sync(self._resolve(storage_key).is_file)

    async def stage(self) -> StagedUpload:
        tmp = self._resolve(".tmp")
        await run_sync(tmp.mkdir, parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=tmp)
        return StagedFile(Path(name), os.fdopen(fd, "wb"))

    async def commit(self, staged: StagedUpload, storage_key: str) -> None:
        assert isinstance(staged, StagedFile)
        try:
            target = self._resolve(storage_key)
            await staged.close()
            if staged.failed:
                raise ValueError("Cannot commit a failed storage write")
            await run_sync(target.parent.mkdir, parents=True, exist_ok=True)
            self._resolve(storage_key)
            await run_sync(os.replace, staged.path, target)
        finally:
            await staged.discard()

    async def discard(self, staged: StagedUpload) -> None:
        assert isinstance(staged, StagedFile)
        await staged.discard()
