"""StorageProvider 单元测试。

通过 StorageProvider 接口注入 LocalStorageProvider，验证写入/读取/删除/存在性；
以及 storage_key 路径穿越防护与 stage/commit/discard 流程。
"""

import hashlib
from pathlib import Path

import pytest

from app.infrastructure.storage.local import LocalStorageProvider
from app.infrastructure.storage.provider import StorageProvider


@pytest.fixture
def provider(tmp_path: Path) -> StorageProvider:
    """注入 LocalStorageProvider，业务视角只持有接口类型。"""
    return LocalStorageProvider(tmp_path)


async def test_save_load_exists_delete_roundtrip(provider: StorageProvider) -> None:
    data = b"hello agentos" * 100
    assert await provider.exists("ab/report.bin") is False

    await provider.save("ab/report.bin", data)
    assert await provider.exists("ab/report.bin") is True
    assert await provider.load("ab/report.bin") == data

    await provider.delete("ab/report.bin")
    assert await provider.exists("ab/report.bin") is False
    # 重复删除静默成功（补偿清理场景）
    await provider.delete("ab/report.bin")


async def test_iter_chunks_streams_content(provider: StorageProvider) -> None:
    data = bytes(range(256)) * 1000  # 256 KB，分多块
    await provider.save("cd/blob.bin", data)

    chunks = [chunk async for chunk in provider.iter_chunks("cd/blob.bin", chunk_size=4096)]
    assert len(chunks) > 1
    assert b"".join(chunks) == data
    assert hashlib.sha256(b"".join(chunks)).hexdigest() == hashlib.sha256(data).hexdigest()


async def test_stage_commit_atomic_and_discard(provider: StorageProvider, tmp_path: Path) -> None:
    staged = await provider.stage()
    await staged.write(b"part-1")
    await staged.write(b"part-2")
    await provider.commit(staged, "ef/final.txt")
    assert await provider.load("ef/final.txt") == b"part-1part-2"
    assert list((tmp_path / ".tmp").iterdir()) == []

    staged2 = await provider.stage()
    await staged2.write(b"junk")
    await provider.discard(staged2)
    assert list((tmp_path / ".tmp").iterdir()) == []
    assert await provider.exists("ef/junk.txt") is False


async def test_rejects_path_traversal_and_absolute_keys(provider: StorageProvider) -> None:
    for bad_key in (
        "../escape.txt", "a/../../escape.txt", "/abs/path.txt", "", "a\\b", ".", "a//b", "C:/file",
    ):
        with pytest.raises(ValueError):
            await provider.exists(bad_key)
        with pytest.raises(ValueError):
            await provider.save(bad_key, b"x")


async def test_open_sequential_lifecycle(provider: StorageProvider) -> None:
    from io import UnsupportedOperation

    async with provider.open("ab/file", "wb") as writer:
        await writer.write(b"abc")
        await writer.write(b"def")
        assert not await provider.exists("ab/file")
        with pytest.raises(UnsupportedOperation):
            await writer.read()
        assert not hasattr(writer, "seek")
    assert writer.closed
    with pytest.raises(ValueError):
        await writer.write(b"late")

    async with provider.open("ab/file", "rb") as reader:
        assert await reader.read(2) == b"ab"
        assert await reader.read(0) == b""
        assert await reader.read() == b"cdef"
        assert await reader.read() == b""
        with pytest.raises(UnsupportedOperation):
            await reader.write(b"x")
        assert not hasattr(reader, "seek")
    assert reader.closed
    with pytest.raises(ValueError):
        await reader.read()


async def test_failed_open_preserves_previous_content(provider: StorageProvider, tmp_path: Path) -> None:
    await provider.save("key", b"previous")
    with pytest.raises(RuntimeError):
        async with provider.open("key", "wb") as writer:
            await writer.write(b"partial")
            raise RuntimeError("aborted")
    assert writer.closed
    assert await provider.load("key") == b"previous"
    assert list((tmp_path / ".tmp").iterdir()) == []


@pytest.mark.parametrize("mode", ["r", "w", "ab", "r+b", "rb+", "", None])
async def test_invalid_open_modes(provider: StorageProvider, mode) -> None:
    with pytest.raises(ValueError, match="mode"):
        async with provider.open("key", mode):
            pass


async def test_containment_symlinks(provider: StorageProvider, tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "secret").write_bytes(b"secret")
    (tmp_path / "escape").symlink_to(outside, target_is_directory=True)
    for operation in (provider.exists, provider.load, provider.delete):
        with pytest.raises(ValueError, match="escapes"):
            await operation("escape/secret")
    with pytest.raises(ValueError, match="escapes"):
        await provider.save("escape/secret", b"overwritten")
    assert (outside / "secret").read_bytes() == b"secret"
    (tmp_path / ".tmp").rmdir()
    (tmp_path / ".tmp").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="escapes"):
        await provider.stage()


async def test_commit_failure_cleans_staging(provider: StorageProvider, tmp_path: Path) -> None:
    staged = await provider.stage()
    await staged.write(b"x")
    with pytest.raises(ValueError):
        await provider.commit(staged, "../escape")
    assert list((tmp_path / ".tmp").iterdir()) == []


async def test_caught_write_failure_cannot_publish(provider: StorageProvider, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="failed storage write"):
        async with provider.open("key", "wb") as writer:
            await writer.write(b"partial")
            with pytest.raises(TypeError):
                await writer.write("not bytes")
    assert not await provider.exists("key")
    assert list((tmp_path / ".tmp").iterdir()) == []
