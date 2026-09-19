"""按需启用的 SDK 集成测试，使用隔离的 MinIO 服务器。"""

import asyncio
import os
import uuid

import pytest

from app.infrastructure.storage.minio import MinioStorageProvider


@pytest.mark.skipif(
    not os.environ.get("STORAGE_TEST_MINIO_ENDPOINT"),
    reason="STORAGE_TEST_MINIO_ENDPOINT must point to an isolated test server",
)
async def test_real_minio_streaming_and_private_bucket_lifecycle():
    bucket = f"storage-test-{uuid.uuid4().hex}"
    storage = MinioStorageProvider(
        os.environ["STORAGE_TEST_MINIO_ENDPOINT"],
        os.environ["STORAGE_TEST_MINIO_ACCESS_KEY"],
        os.environ["STORAGE_TEST_MINIO_SECRET_KEY"],
        bucket,
        secure=False,
    )
    with pytest.raises(RuntimeError, match="does not exist"):
        await storage.exists("key")
    await asyncio.to_thread(storage._client.make_bucket, bucket)
    try:
        content = bytes(range(256)) * (24 * 1024)
        async with storage.open("ab/hash.bin", "wb") as writer:
            for offset in range(0, len(content), 64 * 1024):
                await writer.write(content[offset:offset + 64 * 1024])
            assert not await storage.exists("ab/hash.bin")
        assert not writer.path.exists()
        assert await storage.load("ab/hash.bin") == content
        assert b"".join([part async for part in storage.iter_chunks("ab/hash.bin")]) == content
        with pytest.raises(RuntimeError, match="abort"):
            async with storage.open("ab/hash.bin", "wb") as failed:
                await failed.write(b"partial")
                raise RuntimeError("abort")
        assert not failed.path.exists()
        assert await storage.load("ab/hash.bin") == content
        await storage.delete("ab/hash.bin")
        await storage.delete("ab/hash.bin")
        assert not await storage.exists("ab/hash.bin")
        with pytest.raises(FileNotFoundError):
            await storage.load("ab/hash.bin")
    finally:
        await storage.delete("ab/hash.bin")
        await asyncio.to_thread(storage._client.remove_bucket, bucket)
