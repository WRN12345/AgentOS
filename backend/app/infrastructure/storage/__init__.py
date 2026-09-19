"""存储抽象与本地实现。"""

from app.infrastructure.storage.local import LocalStorageProvider
from app.infrastructure.storage.provider import (
    StagedUpload,
    StorageProvider,
    get_storage_provider,
    storage_for,
)

__all__ = [
    "LocalStorageProvider",
    "StagedUpload",
    "StorageProvider",
    "get_storage_provider",
    "storage_for",
]
