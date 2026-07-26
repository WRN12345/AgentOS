"""统一日志配置。

遵守设计文档第 16 章：日志不记录密码、令牌、API Key 和文件原文。
调用方只应记录用户名、ID、状态等非敏感字段。
"""

import logging
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"

_initialized: set[str] = set()


def setup_logging(process_name: str, log_dir: str | None = None) -> logging.Logger:
    """为进程（backend/worker/scheduler）配置控制台 + 文件日志。"""
    from app.core.config import settings

    directory = Path(log_dir or settings.log_dir)
    directory.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(process_name)
    if process_name in _initialized:
        return logger
    logger.setLevel(logging.INFO)

    formatter = logging.Formatter(_FORMAT)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)

    file_handler = logging.FileHandler(directory / f"{process_name}.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    _initialized.add(process_name)
    return logger
