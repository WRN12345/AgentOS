"""Private MinIO objects using the official synchronous SDK in worker threads."""

import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from minio import Minio
from minio.error import S3Error

from app.infrastructure.storage.provider import StagedUpload, StorageProvider, validate_key
from app.infrastructure.storage.streams import ReadStream, StagedFile, run_sync


def _close_response(response) -> None:
    try:
        response.close()
    finally:
        response.release_conn()


class MinioStorageProvider(StorageProvider):
    backend_name = "minio"

    def __init__(
        self, endpoint: str, access_key: str, secret_key: str, bucket: str, secure: bool = True,
    ) -> None:
        self._client = Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=secure)
        self._bucket = bucket

    async def _call(self, function, *args, **kwargs):
        try:
            return await run_sync(function, *args, **kwargs)
        except S3Error as exc:
            if exc.code == "NoSuchBucket":
                raise RuntimeError(
                    f"MinIO bucket {self._bucket!r} does not exist; provision it before use"
                ) from exc
            raise

    @asynccontextmanager
    async def _open_reader(self, storage_key: str):
        validate_key(storage_key)
        try:
            response = await self._call(
                self._client.get_object, self._bucket, storage_key, on_cancel=_close_response,
            )
        except S3Error as exc:
            if exc.code == "NoSuchKey":
                raise FileNotFoundError(storage_key) from exc
            raise
        reader = ReadStream(response)
        try:
            yield reader
        finally:
            reader.closed = True
            await run_sync(_close_response, response)

    async def exists(self, storage_key: str) -> bool:
        validate_key(storage_key)
        try:
            await self._call(self._client.stat_object, self._bucket, storage_key)
            return True
        except S3Error as exc:
            if exc.code == "NoSuchKey":
                return False
            raise

    async def delete(self, storage_key: str) -> None:
        validate_key(storage_key)
        try:
            await self._call(self._client.remove_object, self._bucket, storage_key)
        except S3Error as exc:
            if exc.code != "NoSuchKey":
                raise

    async def stage(self) -> StagedUpload:
        fd, name = tempfile.mkstemp(prefix="agentos-upload-")
        return StagedFile(Path(name), os.fdopen(fd, "wb"))

    async def commit(self, staged: StagedUpload, storage_key: str) -> None:
        assert isinstance(staged, StagedFile)
        try:
            validate_key(storage_key)
            await staged.close()
            if staged.failed:
                raise ValueError("Cannot commit a failed storage write")
            await self._call(self._client.fput_object, self._bucket, storage_key, str(staged.path))
        finally:
            await staged.discard()

    async def discard(self, staged: StagedUpload) -> None:
        assert isinstance(staged, StagedFile)
        await staged.discard()
