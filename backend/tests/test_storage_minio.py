"""MinIO contract tests using deterministic SDK responses, without an object server."""

import asyncio
import io
import threading
from pathlib import Path

import pytest
from minio.error import S3Error
from pydantic import ValidationError

from app.core.config import Settings
from app.infrastructure.storage import provider as factories
from app.infrastructure.storage.minio import MinioStorageProvider


def s3_error(code: str) -> S3Error:
    return S3Error(code, code, "resource", "request", "host", None)


class Response(io.BytesIO):
    def __init__(self, content: bytes):
        super().__init__(content)
        self.released = False
        self.fail_read = False

    def read(self, size=-1):
        assert threading.current_thread() is not threading.main_thread()
        if self.fail_read:
            raise OSError("stream failure")
        return super().read(size)

    def close(self):
        assert threading.current_thread() is not threading.main_thread()
        super().close()

    def release_conn(self):
        self.released = True


class Client:
    def __init__(self):
        self.objects = {}
        self.responses = []
        self.error = None

    def check(self):
        assert threading.current_thread() is not threading.main_thread()
        if self.error:
            raise self.error

    def get_object(self, bucket, key):
        self.check()
        if key not in self.objects:
            raise s3_error("NoSuchKey")
        response = Response(self.objects[key])
        self.responses.append(response)
        return response

    def stat_object(self, bucket, key):
        self.check()
        if key not in self.objects:
            raise s3_error("NoSuchKey")

    def remove_object(self, bucket, key):
        self.check()
        self.objects.pop(key, None)

    def fput_object(self, bucket, key, path):
        self.check()
        self.objects[key] = Path(path).read_bytes()


@pytest.fixture
def minio(monkeypatch):
    client = Client()
    monkeypatch.setattr("app.infrastructure.storage.minio.Minio", lambda *a, **kw: client)
    return MinioStorageProvider("minio:9000", "access", "secret", "bucket", False), client


async def test_minio_roundtrip_and_lifecycle(minio):
    storage, client = minio
    async with storage.open("ab/file", "wb") as writer:
        await writer.write(b"abc")
        await writer.write(b"def")
        assert "ab/file" not in client.objects
        path = writer.path
    assert writer.closed and not path.exists()
    assert await storage.exists("ab/file")
    async with storage.open("ab/file", "rb") as reader:
        assert await reader.read(2) == b"ab"
        assert await reader.read() == b"cdef"
    assert reader.closed
    assert client.responses[-1].closed and client.responses[-1].released
    assert [chunk async for chunk in storage.iter_chunks("ab/file", 2)] == [b"ab", b"cd", b"ef"]
    assert await storage.load("ab/file") == b"abcdef"
    await storage.delete("ab/file")
    await storage.delete("ab/file")
    assert not await storage.exists("ab/file")
    with pytest.raises(FileNotFoundError):
        await storage.load("ab/file")


async def test_minio_read_failure_closes_and_releases(minio):
    storage, client = minio
    client.objects["key"] = b"content"
    with pytest.raises(OSError, match="stream failure"):
        async with storage.open("key") as reader:
            client.responses[-1].fail_read = True
            await reader.read()
    assert client.responses[-1].closed and client.responses[-1].released


async def test_minio_body_failure_discards(minio):
    storage, client = minio
    with pytest.raises(RuntimeError):
        async with storage.open("key", "wb") as writer:
            await writer.write(b"partial")
            raise RuntimeError("body failure")
    assert writer.closed and not writer.path.exists()
    assert not client.objects


@pytest.mark.parametrize("code", ["AccessDenied", "InvalidAccessKeyId", "InternalError", "NoSuchBucket", "NotFound"])
async def test_minio_errors_propagate_and_upload_failure_cleans(minio, code):
    storage, client = minio
    client.error = s3_error(code)
    expected = RuntimeError if code == "NoSuchBucket" else S3Error
    for operation in (storage.load, storage.exists, storage.delete):
        with pytest.raises(expected):
            await operation("key")
    staged = await storage.stage()
    await staged.write(b"data")
    with pytest.raises(expected):
        await storage.commit(staged, "key")
    assert staged.closed and not staged.path.exists()


@pytest.mark.parametrize("key", ["", "../key", "/key", "a/../b", "a\\b", ".", "a//b", "C:/file", "a\x00b"])
async def test_minio_rejects_invalid_keys(minio, key):
    storage, client = minio
    for operation in (storage.load, storage.exists, storage.delete):
        with pytest.raises(ValueError):
            await operation(key)
    with pytest.raises(ValueError):
        await storage.save(key, b"data")
    staged = await storage.stage()
    with pytest.raises(ValueError):
        await storage.commit(staged, key)
    assert not staged.path.exists()
    assert not client.objects


@pytest.mark.parametrize("during", ["acquire", "read"])
async def test_minio_cancellation_joins_thread_and_releases(minio, monkeypatch, during):
    storage, client = minio
    client.objects["key"] = b"data"
    started, finish = threading.Event(), threading.Event()
    response = Response(b"data")
    client.responses.append(response)

    def blocked(*args):
        started.set()
        assert finish.wait(5)
        return response if during == "acquire" else b"data"

    if during == "acquire":
        monkeypatch.setattr(client, "get_object", blocked)
    else:
        monkeypatch.setattr(client, "get_object", lambda *args: response)
        monkeypatch.setattr(response, "read", blocked)
    task = asyncio.create_task(storage.load("key"))
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    assert not response.closed
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert response.closed and response.released


async def test_minio_cancelled_upload_joins_before_removing_tempfile(minio, monkeypatch):
    storage, client = minio
    started, finish = threading.Event(), threading.Event()
    staged = await storage.stage()
    await staged.write(b"complete")

    def upload(bucket, key, path):
        started.set()
        assert finish.wait(5)
        assert Path(path).read_bytes() == b"complete"

    monkeypatch.setattr(client, "fput_object", upload)
    task = asyncio.create_task(storage.commit(staged, "key"))
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert staged.path.exists()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert staged.closed and not staged.path.exists()


async def test_minio_early_iterator_close_releases_response(minio):
    storage, client = minio
    client.objects["key"] = b"abcdef"
    chunks = storage.iter_chunks("key", 2)
    assert await anext(chunks) == b"ab"
    await chunks.aclose()
    assert client.responses[-1].closed and client.responses[-1].released


async def test_minio_network_errors_propagate(minio):
    storage, client = minio
    client.error = OSError("network unavailable")
    for operation in (storage.load, storage.exists, storage.delete):
        with pytest.raises(OSError, match="network unavailable"):
            await operation("key")


async def test_minio_release_even_when_close_fails(minio, monkeypatch):
    storage, client = minio
    client.objects["key"] = b"data"
    with pytest.raises(OSError, match="close failure"):
        async with storage.open("key"):
            response = client.responses[-1]
            close = response.close

            def fail_close():
                close()
                raise OSError("close failure")

            monkeypatch.setattr(response, "close", fail_close)
    assert response.closed and response.released


def test_settings_and_provider_selection(monkeypatch, tmp_path):
    config = Settings(_env_file=None, storage_backend="local", storage_root=str(tmp_path),
                      minio_endpoint="", minio_access_key="", minio_secret_key="", minio_bucket="")
    monkeypatch.setattr(factories, "settings", config)
    monkeypatch.setattr(factories, "_provider", None)
    monkeypatch.setattr(factories, "_providers", {})
    local = factories.storage_for("local")
    assert factories.get_storage_provider() is local
    with pytest.raises(ValueError, match="MinIO requires"):
        factories.storage_for("minio")
    with pytest.raises(ValueError, match="Unsupported"):
        factories.storage_for("unknown")
    config.minio_endpoint = "minio:9000"
    config.minio_access_key = "access"
    config.minio_secret_key = "secret"
    config.minio_bucket = "bucket"
    remote = factories.storage_for("minio")
    assert factories.storage_for("minio") is remote
    config.storage_backend = "minio"
    assert factories.get_storage_provider() is remote
    assert factories.storage_for("local") is local
    monkeypatch.setattr(factories, "_provider", None)
    assert factories.storage_for("local") is not local


def test_selected_minio_requires_startup_configuration():
    with pytest.raises(ValidationError, match="MinIO requires"):
        Settings(_env_file=None, storage_backend="minio", minio_endpoint="", minio_access_key="",
                 minio_secret_key="", minio_bucket="")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, storage_backend="unsupported")
    with pytest.raises(ValidationError, match="host"):
        Settings(_env_file=None, storage_backend="minio", minio_endpoint="http://minio:9000",
                 minio_access_key="access", minio_secret_key="secret", minio_bucket="bucket")


def test_invalid_settings_hide_credentials():
    secret = "test-secret-that-must-not-appear"
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, storage_backend="minio", minio_endpoint="",
                 minio_secret_key=secret)
    assert secret not in str(error.value)
