"""StorageProvider 单元测试（T4.1 验收，第 14 章）。

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
    # commit：暂存内容原子落位，暂存目录无残留
    staged = await provider.stage()
    await staged.write(b"part-1")
    await staged.write(b"part-2")
    await provider.commit(staged, "ef/final.txt")
    assert await provider.load("ef/final.txt") == b"part-1part-2"
    assert list((tmp_path / ".tmp").iterdir()) == []

    # discard：校验失败场景清理暂存
    staged2 = await provider.stage()
    await staged2.write(b"junk")
    await provider.discard(staged2)
    assert list((tmp_path / ".tmp").iterdir()) == []
    assert await provider.exists("ef/junk.txt") is False


async def test_rejects_path_traversal_and_absolute_keys(provider: StorageProvider) -> None:
    for bad_key in ("../escape.txt", "a/../../escape.txt", "/abs/path.txt", ""):
        with pytest.raises(ValueError):
            await provider.exists(bad_key)
        with pytest.raises(ValueError):
            await provider.save(bad_key, b"x")
