"""按需启用的真实 MinIO 契约测试，使用一次性测试服务器。"""

import hashlib
import os
import uuid

import httpx
import pytest
from minio import Minio

from app.infrastructure.storage.minio import MinioStorageProvider


@pytest.fixture
def live_storage():
    endpoint = os.environ.get("MINIO_TEST_ENDPOINT")
    access_key = os.environ.get("MINIO_TEST_ACCESS_KEY")
    secret_key = os.environ.get("MINIO_TEST_SECRET_KEY")
    if not all((endpoint, access_key, secret_key)):
        pytest.skip("Set MINIO_TEST_ENDPOINT/ACCESS_KEY/SECRET_KEY for a disposable MinIO server")
    secure = os.environ.get("MINIO_TEST_SECURE", "false").lower() == "true"
    bucket = f"agentos-test-{uuid.uuid4().hex}"
    client = Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=secure)
    client.make_bucket(bucket)
    provider = MinioStorageProvider(endpoint, access_key, secret_key, bucket, secure)
    try:
        yield provider, endpoint, bucket, secure
    finally:
        for item in client.list_objects(bucket, recursive=True):
            client.remove_object(bucket, item.object_name)
        client.remove_bucket(bucket)


@pytest.mark.parametrize("size", [0, 6 * 1024 * 1024])
async def test_minio_stream_roundtrip_and_private_access(live_storage, size):
    provider, endpoint, bucket, secure = live_storage
    key = f"projects/{uuid.uuid4()}/files/{uuid.uuid4()}"
    payload = (bytes(range(256)) * (size // 256 + 1))[:size]
    assert not await provider.exists(key)
    async with provider.open(key, "wb") as output:
        for offset in range(0, len(payload), 64 * 1024):
            await output.write(payload[offset:offset + 64 * 1024])
        assert not await provider.exists(key)

    assert await provider.exists(key)
    digest = hashlib.sha256()
    received = 0
    async with provider.open(key, "rb") as stream:
        while chunk := await stream.read(64 * 1024):
            received += len(chunk)
            digest.update(chunk)
    assert received == size
    assert digest.hexdigest() == hashlib.sha256(payload).hexdigest()

    async with httpx.AsyncClient() as client:
        response = await client.get(f"{'https' if secure else 'http'}://{endpoint}/{bucket}/{key}")
        assert response.status_code == 403
    await provider.delete(key)
    await provider.delete(key)
    assert not await provider.exists(key)
    with pytest.raises(FileNotFoundError):
        await provider.load(key)


async def test_minio_aborted_write_is_not_published(live_storage):
    provider, *_ = live_storage
    key = f"aborted/{uuid.uuid4()}"
    with pytest.raises(RuntimeError, match="abort"):
        async with provider.open(key, "wb") as output:
            await output.write(b"unfinished")
            raise RuntimeError("abort")
    assert not await provider.exists(key)
