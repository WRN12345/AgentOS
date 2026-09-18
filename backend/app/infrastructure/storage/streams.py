"""Sequential streams and cancellation-safe threaded storage operations."""

import asyncio
from io import UnsupportedOperation
from pathlib import Path

from app.infrastructure.storage.provider import StagedUpload


async def run_sync(function, *args, on_cancel=None, **kwargs):
    # A thread keeps running after task cancellation. Join it before closing resources.
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    cancelled = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancelled = exc
        except BaseException:
            if cancelled is not None:
                raise cancelled
            raise
    result = task.result()
    if cancelled is not None:
        if on_cancel is not None:
            await run_sync(on_cancel, result)
        raise cancelled
    return result


class ReadStream:
    def __init__(self, stream) -> None:
        self._stream = stream
        self.closed = False

    async def read(self, size: int = -1) -> bytes:
        if self.closed:
            raise ValueError("Read from closed storage stream")
        return await run_sync(self._stream.read, size)

    async def write(self, chunk: bytes) -> None:
        raise UnsupportedOperation("Storage stream is read-only")


class StagedFile(StagedUpload):
    def __init__(self, path: Path, stream) -> None:
        self.path = path
        self._stream = stream
        self.failed = False

    @property
    def closed(self) -> bool:
        return self._stream.closed

    async def read(self, size: int = -1) -> bytes:
        raise UnsupportedOperation("Storage stream is write-only")

    async def write(self, chunk: bytes) -> None:
        try:
            await run_sync(self._stream.write, chunk)
        except BaseException:
            self.failed = True
            raise

    async def close(self) -> None:
        await run_sync(self._stream.close)

    async def discard(self) -> None:
        try:
            await self.close()
        finally:
            await run_sync(self.path.unlink, missing_ok=True)
