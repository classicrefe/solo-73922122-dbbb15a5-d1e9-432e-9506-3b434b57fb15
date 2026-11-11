"""运行时配置。

所有配置均通过环境变量读取，便于同一镜像在 Docker Compose 中以不同角色
（两个 API 进程 / 本地银行源）运行。读取函数在每次调用时求值，测试可以
通过修改环境变量来隔离数据目录。
"""

from __future__ import annotations

import os
from pathlib import Path

# 单份回单的硬上限：16 MiB（含 16 MiB 本身，超过即拒绝）
MAX_RECEIPT_BYTES = 16 * 1024 * 1024

# 逐块下载的块大小（仅在内存中保留一个块）
DOWNLOAD_CHUNK_SIZE = 256 * 1024

DEFAULT_BANK_RECEIPT_BASE_URL = "http://127.0.0.1:9000"
# Docker 镜像中通过 ENV/Compose 覆盖为 /data；本地直接运行时默认落到 ./data
DEFAULT_DATA_DIR = "./data"

# 支持多个环境变量名，便于部署时沿用不同平台的命名习惯
_BANK_URL_ENV_NAMES = (
    "BANK_RECEIPT_BASE_URL",
    "BANK_SOURCE_BASE_URL",
    "BANK_SOURCE_URL",
    "BANK_RECEIPT_URL",
)

_DATA_DIR_ENV_NAMES = ("RECEIPT_DATA_DIR", "RECEIPT_STORE_DIR")


def bank_receipt_base_url() -> str:
    """配置固定的银行回单 HTTP 源（不带末尾斜杠）。"""
    for name in _BANK_URL_ENV_NAMES:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip().rstrip("/")
    return DEFAULT_BANK_RECEIPT_BASE_URL


def receipt_data_dir() -> Path:
    """回单数据根目录：SQLite 索引 + 本地原件对象都放在这里。"""
    for name in _DATA_DIR_ENV_NAMES:
        value = os.getenv(name)
        if value and value.strip():
            return Path(value).expanduser().resolve()
    return Path(DEFAULT_DATA_DIR).expanduser().resolve()


def db_path() -> Path:
    return receipt_data_dir() / "receipts.db"


def objects_dir() -> Path:
    # 原件按内容寻址（文件名 = SHA-256），同一摘要天然复用同一份文件
    return receipt_data_dir() / "objects"


def staging_dir() -> Path:
    # 每个批次独立暂存目录；进程中断只会留下不可见、未被索引的临时文件
    return receipt_data_dir() / "staging"


def http_connect_timeout_seconds() -> float:
    return float(os.getenv("BANK_RECEIPT_CONNECT_TIMEOUT", "10"))


def http_read_timeout_seconds() -> float:
    return float(os.getenv("BANK_RECEIPT_READ_TIMEOUT", "60"))
