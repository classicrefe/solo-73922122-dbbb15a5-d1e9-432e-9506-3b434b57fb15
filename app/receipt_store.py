"""SQLite 索引 + 内容寻址的本地原件存储。

设计要点
========
* SQLite **只存索引**：流水 ID、SHA-256、字节数、归档时间。原件字节永远
  不写入数据库。
* 原件以 SHA-256 命名保存在 ``objects/`` 下，天然实现同摘要共享与幂等复用。
* 每批下载先写入 ``staging/<batch_id>/``；全部校验通过后才把暂存文件
  ``os.replace`` 到 ``objects/``，随后用**单个 SQLite 写事务**一次性插入整批
  索引。查询端只认索引，因此：
    - 断流 / 超限 / 摘要错误：事务回滚，整批不可见；
    - 进程在发布前中断：最多留下未索引的孤儿文件，绝不可见；
    - 进程在事务提交后中断：索引与文件都已完整落盘，整批可见。
* 网络等待期间不持有任何数据库写锁；``BEGIN IMMEDIATE`` 仅在最后提交索引
  的极短时间内持有。
* 失败清理只删除本批的 staging 目录，从不删除 ``objects/`` 中可能已被其它
  批次发布（或正在发布）的共享文件。
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .config import db_path, objects_dir, receipt_data_dir, staging_dir

# 进程内已初始化过的数据库路径；DDL 不放在每次请求的热路径上
_init_lock = threading.Lock()
_initialized_paths: set[str] = set()


class ReceiptConflict(Exception):
    """同一流水 ID 已归档但摘要不同。"""

    def __init__(
        self, transaction_id: str, existing_sha256: str, requested_sha256: str
    ) -> None:
        super().__init__(f"流水 {transaction_id} 已归档为不同摘要")
        self.transaction_id = transaction_id
        self.existing_sha256 = existing_sha256
        self.requested_sha256 = requested_sha256

    def detail(self) -> dict:
        return {
            "code": "receipt_conflict",
            "message": "同一流水 ID 已归档为不同摘要，整批拒绝",
            "transaction_id": self.transaction_id,
            "existing_sha256": self.existing_sha256,
            "expected_sha256": self.requested_sha256,
            "batch_rejected": True,
        }


class ReceiptStoreBusy(Exception):
    """SQLite 在重试后仍被其它写事务占用。"""


def object_path(sha256: str) -> Path:
    return objects_dir() / sha256.lower()


def _connect() -> sqlite3.Connection:
    # 每次操作独立连接，避免跨进程 / 跨线程共享连接；WAL 让读不阻塞写。
    conn = sqlite3.connect(db_path(), timeout=30.0, isolation_level=None)
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


def ensure_initialized() -> None:
    """创建数据目录与索引表；可被两个 API 进程并发调用。

    同进程内只执行一次 DDL，避免在每次请求（尤其是下载等待前后的读路径）上
    反复申请 schema 锁。
    """
    data = receipt_data_dir()
    data.mkdir(parents=True, exist_ok=True)
    objects_dir().mkdir(parents=True, exist_ok=True)
    staging_dir().mkdir(parents=True, exist_ok=True)

    key = str(db_path())
    if key in _initialized_paths and db_path().exists():
        return

    with _init_lock:
        if key in _initialized_paths and db_path().exists():
            return
        conn = _connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS receipts (
                    transaction_id TEXT PRIMARY KEY,
                    sha256         TEXT NOT NULL,
                    size_bytes     INTEGER NOT NULL,
                    created_at     TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_receipts_sha256 ON receipts(sha256)"
            )
            # WAL 模式会持久化到数据库文件；后续连接自动继承，无需每次设置
            conn.execute("PRAGMA journal_mode = WAL")
        finally:
            conn.close()
        _initialized_paths.add(key)


def lookup_receipts(transaction_ids: list[str]) -> dict[str, dict]:
    """批量读取索引（只读，读完立刻关闭连接，不参与后续网络等待）。"""
    if not transaction_ids:
        return {}
    ensure_initialized()
    conn = _connect()
    try:
        placeholders = ",".join("?" for _ in transaction_ids)
        rows = conn.execute(
            f"SELECT transaction_id, sha256, size_bytes, created_at "
            f"FROM receipts WHERE transaction_id IN ({placeholders})",
            tuple(transaction_ids),
        ).fetchall()
    finally:
        conn.close()
    return {
        row[0]: {
            "transaction_id": row[0],
            "sha256": row[1],
            "size_bytes": row[2],
            "created_at": row[3],
        }
        for row in rows
    }


def get_receipt(transaction_id: str) -> dict | None:
    ensure_initialized()
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT transaction_id, sha256, size_bytes, created_at "
            "FROM receipts WHERE transaction_id = ?",
            (transaction_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return {
        "transaction_id": row[0],
        "sha256": row[1],
        "size_bytes": row[2],
        "created_at": row[3],
    }


def list_receipts() -> list[dict]:
    ensure_initialized()
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT transaction_id, sha256, size_bytes, created_at "
            "FROM receipts ORDER BY created_at ASC, transaction_id ASC"
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "transaction_id": row[0],
            "sha256": row[1],
            "size_bytes": row[2],
            "created_at": row[3],
        }
        for row in rows
    ]


def new_staging_dir(batch_id: str) -> Path:
    path = staging_dir() / batch_id
    path.mkdir(parents=True, exist_ok=False)
    return path


def discard_staging(path: Path | None) -> None:
    """清理本批暂存目录；绝不触碰 objects/ 中已发布或他批的文件。"""
    if path is None:
        return
    shutil.rmtree(path, ignore_errors=True)


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def publish_object(staged_path: Path, sha256: str) -> Path:
    """把已校验的暂存文件放入内容寻址对象目录。

    若目标已存在（同摘要已被其它批次发布），直接丢弃暂存副本；绝不删除或
    覆盖目标文件，因此并发失败清理不会破坏另一批已发布的回单。
    """
    target = object_path(sha256)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        try:
            staged_path.unlink()
        except FileNotFoundError:
            pass
        return target

    # 同文件系统内的原子替换；先 fsync 文件内容，再 fsync 目录项
    os.replace(staged_path, target)
    try:
        with target.open("rb") as handle:
            os.fsync(handle.fileno())
    except OSError:
        pass
    _fsync_dir(target.parent)
    return target


def publish_index(entries: list[dict]) -> dict[str, bool]:
    """在单个写事务中原子发布整批索引。

    返回 ``{transaction_id: reused}``。提交前会在写事务内再次检查既有索引，
    以覆盖两个进程并发处理重叠 ID 的竞态：
      * 既有摘要相同 -> 复用，不重复插入；
      * 既有摘要不同 -> 抛出 ReceiptConflict，整批回滚。
    ``BEGIN IMMEDIATE`` 只在此函数内持有，之前的下载阶段不涉及任何写锁。
    """
    ensure_initialized()
    last_error: Exception | None = None
    for attempt in range(6):
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            reused: dict[str, bool] = {}
            for entry in entries:
                transaction_id = entry["transaction_id"]
                sha256 = entry["sha256"].lower()
                row = conn.execute(
                    "SELECT sha256 FROM receipts WHERE transaction_id = ?",
                    (transaction_id,),
                ).fetchone()
                if row is not None:
                    if row[0].lower() != sha256:
                        raise ReceiptConflict(transaction_id, row[0], sha256)
                    reused[transaction_id] = True
                    continue

                conn.execute(
                    "INSERT INTO receipts "
                    "(transaction_id, sha256, size_bytes, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        transaction_id,
                        sha256,
                        int(entry["size_bytes"]),
                        datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    ),
                )
                reused[transaction_id] = False

            conn.execute("COMMIT")
            return reused
        except ReceiptConflict:
            _rollback_quietly(conn)
            raise
        except sqlite3.OperationalError as exc:
            _rollback_quietly(conn)
            last_error = exc
            message = str(exc).lower()
            if ("locked" in message or "busy" in message) and attempt < 5:
                time.sleep(0.05 * (attempt + 1))
                continue
            raise ReceiptStoreBusy(
                f"SQLite 写事务失败（已重试 {attempt + 1} 次）: {exc}"
            ) from exc
        except BaseException:
            _rollback_quietly(conn)
            raise
        finally:
            conn.close()
    raise ReceiptStoreBusy(f"SQLite 写事务持续繁忙: {last_error}")


def _rollback_quietly(conn: sqlite3.Connection) -> None:
    try:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def new_batch_id() -> str:
    return uuid.uuid4().hex
