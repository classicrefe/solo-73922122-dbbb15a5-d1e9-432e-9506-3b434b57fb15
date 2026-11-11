"""本地银行回单源（用于演示与故障模拟）。

提供与生产银行源相同的 ``GET /receipts/{id}`` 接口，并额外提供：

* ``GET  /receipts``                    —— 回单清单（含预期 SHA-256）；
* ``PUT  /admin/receipts/{id}``         —— 新建/覆盖回单内容；
* ``POST /admin/receipts/{id}/mode``    —— 切换正常 / 断流 / 坏摘要 / 超限模式；
* ``GET  /admin/stats``                 —— 各回单被下载次数（便于验证复用）；
* ``POST /admin/reset``                 —— 恢复内置演示数据。

故障模式：
  * ``truncate``   —— 声明完整 Content-Length，只发送一半后中断连接；
  * ``bad_digest`` —— 返回被篡改的字节，摘要必然与预期不符；
  * ``oversize``   —— 不声明长度，持续发送超过 16 MiB 的数据；
  * ``slow``       —— 慢速正常发送，用于演示下载期间不持有数据库写锁。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import threading
from dataclasses import dataclass
from typing import Literal

from fastapi import APIRouter, FastAPI, HTTPException, Path
from fastapi.responses import RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

MAX_RECEIPT_BYTES = 16 * 1024 * 1024
ID_PATTERN = r"^[A-Za-z0-9-]+$"
Mode = Literal["normal", "truncate", "bad_digest", "oversize", "slow"]

ReceiptId = Path(min_length=1, pattern=ID_PATTERN)

app = FastAPI(
    title="本地银行回单源（演示 / 故障模拟）",
    version="1.0.0",
    description=(
        "模拟银行回单 HTTP 源。`GET /receipts/{id}` 返回原件字节；"
        "通过 `/admin/receipts/{id}/mode` 可切换断流、坏摘要、超限等故障模式。"
    ),
)

router = APIRouter(tags=["银行源"])
_lock = threading.Lock()


@dataclass
class _Receipt:
    content: bytes
    mode: Mode = "normal"


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _default_receipts() -> dict[str, _Receipt]:
    return {
        "RCPT-DEMO-001": _Receipt(
            (
                "BANK RECEIPT\n"
                "transaction-id: RCPT-DEMO-001\n"
                "amount: 15800 CNY\n"
                "value-date: 2026-11-02\n"
                "status: SETTLED\n"
            ).encode("utf-8")
        ),
        "RCPT-DEMO-002": _Receipt(
            (
                "BANK RECEIPT\n"
                "transaction-id: RCPT-DEMO-002\n"
                "amount: 4200 CNY\n"
                "value-date: 2026-11-03\n"
                "status: SETTLED\n"
            ).encode("utf-8")
        ),
        # 断流演示：客户端会收到完整 Content-Length，但连接在中途断开
        "RCPT-DEMO-TRUNC": _Receipt(
            (
                "BANK RECEIPT\n"
                "transaction-id: RCPT-DEMO-TRUNC\n"
                "amount: 900 CNY\n"
                "value-date: 2026-11-04\n"
                "status: SETTLED\n"
                "note: this response will be truncated\n"
            ).encode("utf-8"),
            mode="truncate",
        ),
        # 坏摘要演示：源端返回被篡改的字节
        "RCPT-DEMO-BAD": _Receipt(
            (
                "BANK RECEIPT\n"
                "transaction-id: RCPT-DEMO-BAD\n"
                "amount: 700 CNY\n"
                "value-date: 2026-11-05\n"
                "status: SETTLED\n"
            ).encode("utf-8"),
            mode="bad_digest",
        ),
        # 超限演示：不声明长度，持续发送 > 16 MiB
        "RCPT-DEMO-BIG": _Receipt(
            b"OVERSIZE RECEIPT PLACEHOLDER\n", mode="oversize"
        ),
    }


_records: dict[str, _Receipt] = _default_receipts()
_stats: dict[str, int] = {}


class ReceiptUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str | None = Field(
        default=None, description="回单文本内容（UTF-8）；与 content_base64 二选一"
    )
    content_base64: str | None = Field(
        default=None, description="回单原始字节的 Base64；与 content 二选一"
    )
    mode: Mode = Field(default="normal", description="故障模拟模式")


class ModeUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Mode


def _metadata(receipt_id: str, receipt: _Receipt) -> dict:
    if receipt.mode == "oversize":
        size_bytes = MAX_RECEIPT_BYTES + 1
        sha = None
    else:
        size_bytes = len(receipt.content)
        sha = _sha256(receipt.content)
    return {
        "transaction_id": receipt_id,
        "mode": receipt.mode,
        "size_bytes": size_bytes,
        "sha256": sha,
        "download_url": f"/receipts/{receipt_id}",
    }


@router.get("/health", summary="银行源健康检查")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/receipts", summary="回单清单（含预期 SHA-256）")
def manifest() -> dict:
    with _lock:
        items = [_metadata(rid, rec) for rid, rec in _records.items()]
    return {
        "receipts": items,
        "note": (
            "collect 时 sha256 使用此处的值；若某条回单为 truncate/bad_digest/"
            "oversize 模式，采集会按设计失败并整批回滚"
        ),
    }


async def _generator_oversize():
    chunk = b"X" * (256 * 1024)
    sent = 0
    while sent <= MAX_RECEIPT_BYTES:
        yield chunk
        sent += len(chunk)


async def _generator_truncate(content: bytes):
    half = max(1, len(content) // 2)
    yield content[:half]
    await asyncio.sleep(0.02)
    # 故意不发送剩余字节，也不发送结束分块；客户端应判定流中断
    raise RuntimeError("simulated upstream stream interruption")


async def _generator_slow(content: bytes):
    # 慢速分块发送，便于演示下载等待期间其它事务不被写锁阻塞
    chunk_size = 32
    for offset in range(0, len(content), chunk_size):
        await asyncio.sleep(0.25)
        yield content[offset : offset + chunk_size]


@router.get("/receipts/{receipt_id}", summary="下载回单原件（可模拟断流/坏摘要/超限）")
def get_receipt(receipt_id: str = ReceiptId):
    with _lock:
        receipt = _records.get(receipt_id)
        if receipt is not None:
            _stats[receipt_id] = _stats.get(receipt_id, 0) + 1
    if receipt is None:
        raise HTTPException(status_code=404, detail="receipt not found")

    if receipt.mode == "oversize":
        return StreamingResponse(
            _generator_oversize(), media_type="application/octet-stream"
        )

    content = receipt.content

    if receipt.mode == "truncate":
        return StreamingResponse(
            _generator_truncate(content),
            media_type="application/octet-stream",
            headers={"Content-Length": str(len(content))},
        )

    if receipt.mode == "slow":
        return StreamingResponse(
            _generator_slow(content),
            media_type="application/octet-stream",
            headers={"Content-Length": str(len(content))},
        )

    if receipt.mode == "bad_digest":
        corrupted = content + b"\nCORRUPTED-BY-BANK-SOURCE\n"
        return Response(
            content=corrupted,
            media_type="application/octet-stream",
            headers={"X-Receipt-Sha256": _sha256(corrupted)},
        )

    return Response(
        content=content,
        media_type="application/octet-stream",
        headers={"X-Receipt-Sha256": _sha256(content)},
    )


# ---------------------------------------------------------------------------
# 管理接口（仅本地演示源；生产银行源不会提供）
# ---------------------------------------------------------------------------


@router.get("/admin/receipts", summary="查看全部回单（含故障模式）")
def admin_list() -> dict:
    with _lock:
        return {
            "receipts": [_metadata(rid, rec) for rid, rec in _records.items()]
        }


@router.put("/admin/receipts/{receipt_id}", summary="新建或覆盖回单")
def admin_upsert(payload: ReceiptUpsert, receipt_id: str = ReceiptId):
    if payload.content is not None and payload.content_base64 is not None:
        raise HTTPException(
            status_code=422, detail="content 与 content_base64 只能提供一个"
        )
    if payload.content_base64 is not None:
        try:
            content = base64.b64decode(payload.content_base64, validate=True)
        except Exception as exc:  # noqa: BLE001 - 明确转换为 422
            raise HTTPException(status_code=422, detail=f"content_base64 无效: {exc}")
    elif payload.content is not None:
        content = payload.content.encode("utf-8")
    else:
        with _lock:
            existing = _records.get(receipt_id)
        content = existing.content if existing else b""

    with _lock:
        _records[receipt_id] = _Receipt(content=content, mode=payload.mode)
        meta = _metadata(receipt_id, _records[receipt_id])
    return meta


@router.post("/admin/receipts/{receipt_id}/mode", summary="切换故障模式")
def admin_set_mode(payload: ModeUpdate, receipt_id: str = ReceiptId):
    with _lock:
        receipt = _records.get(receipt_id)
        if receipt is None:
            raise HTTPException(status_code=404, detail="receipt not found")
        receipt.mode = payload.mode
        return _metadata(receipt_id, receipt)


@router.delete("/admin/receipts/{receipt_id}", summary="删除回单")
def admin_delete(receipt_id: str = ReceiptId):
    with _lock:
        if receipt_id not in _records:
            raise HTTPException(status_code=404, detail="receipt not found")
        del _records[receipt_id]
        _stats.pop(receipt_id, None)
    return {"deleted": receipt_id}


@router.get("/admin/stats", summary="各回单被下载次数")
def admin_stats() -> dict:
    with _lock:
        return {"receipt_gets": dict(_stats)}


@router.post("/admin/reset", summary="恢复内置演示回单")
def admin_reset() -> dict:
    global _records, _stats
    with _lock:
        _records = _default_receipts()
        _stats = {}
        return {
            "receipts": [_metadata(rid, rec) for rid, rec in _records.items()]
        }


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


app.include_router(router)
