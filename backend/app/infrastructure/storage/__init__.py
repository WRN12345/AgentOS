"""存储抽象与本地实现（第 14 章）。"""

from app.infrastructure.storage.local import LocalStorageProvider
from app.infrastructure.storage.provider import (
    StagedUpload,
    StorageProvider,
    get_storage_provider,
)

__all__ = [
    "LocalStorageProvider",
    "StagedUpload",
    "StorageProvider",
    "get_storage_provider",
]
