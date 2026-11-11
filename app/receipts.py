"""回单采集与原件下载路由。"""

from __future__ import annotations

from pathlib import Path as FsPath
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path
from fastapi.responses import FileResponse

from . import receipt_store as store
from .config import bank_receipt_base_url
from .receipt_fetch import (
    FetchResult,
    ReceiptFetchFailure,
    download_and_verify,
)
from .receipt_schemas import (
    ID_PATTERN,
    CollectedReceipt,
    ReceiptCollectRequest,
    ReceiptCollectResponse,
    ReceiptListResponse,
    ReceiptMetadata,
)

router = APIRouter(tags=["回单采集"])

_TX_PATH = Annotated[
    str,
    Path(
        min_length=1,
        pattern=ID_PATTERN,
        description="银行回单索引（流水 ID）",
    ),
]


def _download_url(transaction_id: str) -> str:
    return f"/api/v1/receipts/{transaction_id}"


def _metadata_payload(row: dict) -> dict:
    return {
        "transaction_id": row["transaction_id"],
        "sha256": row["sha256"],
        "size_bytes": row["size_bytes"],
        "created_at": row["created_at"],
        "download_url": _download_url(row["transaction_id"]),
    }


def _get_or_404(transaction_id: str) -> dict:
    row = store.get_receipt(transaction_id)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "receipt_not_found",
                "message": f"流水 {transaction_id} 尚未归档",
                "transaction_id": transaction_id,
            },
        )
    return row


def _collect(payload: ReceiptCollectRequest) -> ReceiptCollectResponse:
    """采集一批回单：先全部下载校验，再一次原子发布。"""
    store.ensure_initialized()
    if not bank_receipt_base_url():
        raise HTTPException(
            status_code=503,
            detail={
                "code": "bank_source_not_configured",
                "message": "未配置银行回单 HTTP 源",
                "batch_rejected": True,
            },
        )

    batch_id = store.new_batch_id()
    requested_ids = [item.transaction_id for item in payload.items]

    # ---------- 1. 只读预检：已有的同摘要直接复用，异摘要立即整批拒绝 ----------
    existing = store.lookup_receipts(requested_ids)
    for item in payload.items:
        row = existing.get(item.transaction_id)
        if row is not None and row["sha256"].lower() != item.sha256:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "receipt_conflict",
                    "message": "同一流水 ID 已归档为不同摘要，整批拒绝",
                    "transaction_id": item.transaction_id,
                    "existing_sha256": row["sha256"],
                    "expected_sha256": item.sha256,
                    "batch_rejected": True,
                },
            )

    # 已归档且原件文件仍存在 -> 无需联网；同 ID 同摘要复用
    to_fetch = [
        item
        for item in payload.items
        if item.transaction_id not in existing
        or not store.object_path(existing[item.transaction_id]["sha256"]).exists()
    ]

    staging: FsPath | None = None
    # transaction_id -> (暂存文件, 校验结果)
    staged: dict[str, tuple[FsPath, FetchResult]] = {}

    try:
        # ---------- 2. 全部网络下载在写事务之外进行 ----------
        if to_fetch:
            staging = store.new_staging_dir(batch_id)
            for index, item in enumerate(to_fetch):
                staged_path = staging / f"{index:02d}.part"
                result = download_and_verify(
                    item.transaction_id, item.sha256, staged_path
                )
                staged[item.transaction_id] = (staged_path, result)

        # ---------- 3. 校验全部通过后才把原件放入共享对象目录 ----------
        for transaction_id, (staged_path, result) in staged.items():
            store.publish_object(staged_path, result.sha256)

        # ---------- 4. 单事务原子发布整批索引 ----------
        entries = []
        for item in payload.items:
            result = staged.get(item.transaction_id)
            if result is not None:
                size_bytes = result[1].size_bytes
            else:
                size_bytes = existing[item.transaction_id]["size_bytes"]
            entries.append(
                {
                    "transaction_id": item.transaction_id,
                    "sha256": item.sha256,
                    "size_bytes": size_bytes,
                }
            )
        reused_map = store.publish_index(entries)
    except ReceiptFetchFailure as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail()) from exc
    except store.ReceiptConflict as exc:
        raise HTTPException(status_code=409, detail=exc.detail()) from exc
    except store.ReceiptStoreBusy as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "store_busy",
                "message": str(exc),
                "batch_rejected": True,
            },
        ) from exc
    finally:
        # 只删除本批暂存目录；objects/ 中的共享文件从不在此清理
        store.discard_staging(staging)

    receipts: list[CollectedReceipt] = []
    published = 0
    reused = 0
    for item in payload.items:
        is_reused = reused_map[item.transaction_id]
        if is_reused:
            reused += 1
        else:
            published += 1
        result = staged.get(item.transaction_id)
        size_bytes = (
            result[1].size_bytes
            if result is not None
            else existing[item.transaction_id]["size_bytes"]
        )
        receipts.append(
            CollectedReceipt(
                transaction_id=item.transaction_id,
                sha256=item.sha256,
                size_bytes=size_bytes,
                reused=is_reused,
                download_url=_download_url(item.transaction_id),
            )
        )

    return ReceiptCollectResponse(
        batch_id=batch_id,
        total=len(payload.items),
        published=published,
        reused=reused,
        receipts=receipts,
    )


_COLLECT_DESCRIPTION = (
    "接收 1~12 项流水 ID 及预期 SHA-256。服务从配置固定的银行 HTTP 源 "
    "`GET /receipts/{id}` 逐块下载，每份上限 16 MiB，核验摘要后把原件存入"
    "本地内容寻址目录，SQLite 只保存索引。\n\n"
    "**整批原子性**：本批全部成功才同时发布；断流、超限、摘要错误、进程"
    "中断都不会暴露半批。同 ID 同摘要复用已归档原件；同 ID 异摘要以 409 "
    "拒绝整批。"
)


@router.post(
    "/api/v1/receipts",
    response_model=ReceiptCollectResponse,
    summary="采集并原子发布一批银行回单",
    description=_COLLECT_DESCRIPTION,
    responses={
        409: {"description": "同 ID 已归档为不同摘要，整批拒绝"},
        413: {"description": "单份回单超过 16 MiB，整批拒绝"},
        422: {"description": "请求校验失败或下载内容摘要不符，整批拒绝"},
        502: {"description": "银行源不可达或响应流中断，整批拒绝"},
        503: {"description": "索引存储暂时繁忙，整批拒绝"},
    },
)
@router.post(
    "/api/v1/receipts/collect",
    response_model=ReceiptCollectResponse,
    summary="采集并原子发布一批银行回单（显式 collect 路径）",
    description=_COLLECT_DESCRIPTION,
    include_in_schema=True,
)
@router.post(
    "/api/v1/receipts/batch",
    response_model=ReceiptCollectResponse,
    summary="采集并原子发布一批银行回单（batch 别名）",
    description=_COLLECT_DESCRIPTION,
    include_in_schema=False,
)
def collect_receipts(payload: ReceiptCollectRequest) -> ReceiptCollectResponse:
    return _collect(payload)


@router.get(
    "/api/v1/receipts",
    response_model=ReceiptListResponse,
    summary="列出已归档回单索引",
)
def list_archived_receipts() -> ReceiptListResponse:
    rows = store.list_receipts()
    return ReceiptListResponse(
        total=len(rows),
        receipts=[ReceiptMetadata(**_metadata_payload(row)) for row in rows],
    )


@router.get(
    "/api/v1/receipts/{transaction_id}/metadata",
    response_model=ReceiptMetadata,
    summary="按流水 ID 查询已归档回单元数据",
)
@router.get(
    "/api/v1/receipts/{transaction_id}/info",
    response_model=ReceiptMetadata,
    summary="按流水 ID 查询已归档回单元数据（info 别名）",
    include_in_schema=False,
)
def receipt_metadata(transaction_id: _TX_PATH) -> ReceiptMetadata:
    row = _get_or_404(transaction_id)
    return ReceiptMetadata(**_metadata_payload(row))


def _serve_receipt(transaction_id: str) -> FileResponse:
    row = _get_or_404(transaction_id)
    path = store.object_path(row["sha256"])
    if not path.is_file():
        # 索引存在但原件缺失：属于存储完整性问题，明确报错而不是静默 404
        raise HTTPException(
            status_code=500,
            detail={
                "code": "receipt_object_missing",
                "message": f"流水 {transaction_id} 的已索引原件缺失",
                "transaction_id": transaction_id,
                "sha256": row["sha256"],
            },
        )
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=f"{transaction_id}.bin",
        headers={
            "X-Receipt-Sha256": row["sha256"],
            "X-Receipt-Size": str(row["size_bytes"]),
        },
    )


@router.get(
    "/api/v1/receipts/{transaction_id}/download",
    summary="下载已归档的银行回单原件",
    response_class=FileResponse,
)
@router.get(
    "/api/v1/receipts/{transaction_id}",
    summary="按流水 ID 下载已归档的银行回单原件",
    response_class=FileResponse,
)
def download_receipt(transaction_id: _TX_PATH) -> FileResponse:
    return _serve_receipt(transaction_id)


# ---------------------------------------------------------------------------
# 不带 /api/v1 前缀的兼容别名（不出现在 OpenAPI 文档中，避免扰乱演示）
# ---------------------------------------------------------------------------


@router.post(
    "/receipts",
    response_model=ReceiptCollectResponse,
    include_in_schema=False,
)
@router.post(
    "/receipts/collect",
    response_model=ReceiptCollectResponse,
    include_in_schema=False,
)
def collect_receipts_root_alias(payload: ReceiptCollectRequest) -> ReceiptCollectResponse:
    return _collect(payload)


@router.get("/receipts", response_model=ReceiptListResponse, include_in_schema=False)
def list_archived_receipts_root_alias() -> ReceiptListResponse:
    rows = store.list_receipts()
    return ReceiptListResponse(
        total=len(rows),
        receipts=[ReceiptMetadata(**_metadata_payload(row)) for row in rows],
    )


@router.get(
    "/receipts/{transaction_id}",
    response_class=FileResponse,
    include_in_schema=False,
)
def download_receipt_root_alias(transaction_id: _TX_PATH) -> FileResponse:
    return _serve_receipt(transaction_id)
