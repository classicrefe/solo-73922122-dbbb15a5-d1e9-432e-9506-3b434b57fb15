"""从固定银行 HTTP 源逐块下载回单并核验 SHA-256。"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import httpx

from .config import (
    DOWNLOAD_CHUNK_SIZE,
    MAX_RECEIPT_BYTES,
    bank_receipt_base_url,
    http_connect_timeout_seconds,
    http_read_timeout_seconds,
)


class ReceiptFetchFailure(Exception):
    """下载 / 校验阶段的可预期失败。

    ``status_code`` 是建议返回给调用方的 HTTP 状态码；``code`` 是稳定的机器
    可读错误码；``transaction_id`` 指出批内哪一项失败。
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        transaction_id: str | None = None,
        extra: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.transaction_id = transaction_id
        self.extra = extra or {}

    def detail(self) -> dict:
        payload = {
            "code": self.code,
            "message": self.message,
            "batch_rejected": True,
        }
        if self.transaction_id is not None:
            payload["transaction_id"] = self.transaction_id
        payload.update(self.extra)
        return payload


class UpstreamFailure(ReceiptFetchFailure):
    def __init__(self, transaction_id: str, message: str, *, extra: dict | None = None):
        super().__init__(
            502, "upstream_failure", message, transaction_id=transaction_id, extra=extra
        )


class StreamInterrupted(ReceiptFetchFailure):
    def __init__(self, transaction_id: str, message: str):
        super().__init__(
            502, "stream_interrupted", message, transaction_id=transaction_id
        )


class ReceiptTooLarge(ReceiptFetchFailure):
    def __init__(self, transaction_id: str, size: int) -> None:
        super().__init__(
            413,
            "receipt_too_large",
            f"回单超过单份上限 {MAX_RECEIPT_BYTES} 字节",
            transaction_id=transaction_id,
            extra={"max_bytes": MAX_RECEIPT_BYTES, "actual_bytes_at_least": size},
        )


class DigestMismatch(ReceiptFetchFailure):
    def __init__(
        self, transaction_id: str, expected: str, actual: str, size: int
    ) -> None:
        super().__init__(
            422,
            "digest_mismatch",
            "下载内容的 SHA-256 与预期不一致，整批拒绝",
            transaction_id=transaction_id,
            extra={
                "expected_sha256": expected,
                "actual_sha256": actual,
                "size_bytes": size,
            },
        )


@dataclass(frozen=True)
class FetchResult:
    sha256: str
    size_bytes: int


def _receipt_url(transaction_id: str) -> str:
    base = bank_receipt_base_url()
    return f"{base}/receipts/{quote(transaction_id, safe='-')}"


def download_and_verify(
    transaction_id: str, expected_sha256: str, destination: Path
) -> FetchResult:
    """逐块下载单份回单到 ``destination``，校验大小与摘要。

    只把数据写入调用方提供的暂存路径；本函数不接触 SQLite，也不发布任何
    文件。任何异常都意味着该份回单不可用，由上层将整批回滚。
    """
    expected = expected_sha256.lower()
    url = _receipt_url(transaction_id)
    timeout = httpx.Timeout(
        connect=http_connect_timeout_seconds(),
        read=http_read_timeout_seconds(),
        write=http_read_timeout_seconds(),
        pool=http_connect_timeout_seconds(),
    )

    digest = hashlib.sha256()
    size = 0
    declared_length: int | None = None

    destination.parent.mkdir(parents=True, exist_ok=True)

    try:
        # 固定源、不跟随重定向、要求 identity 编码，确保摘要针对原始字节
        with httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            headers={"Accept-Encoding": "identity"},
        ) as client:
            with client.stream("GET", url) as response:
                if response.status_code != 200:
                    raise UpstreamFailure(
                        transaction_id,
                        f"银行源返回 HTTP {response.status_code}（期望 200）",
                        extra={"upstream_status": response.status_code},
                    )

                raw_length = response.headers.get("content-length")
                if raw_length is not None:
                    try:
                        declared_length = int(raw_length)
                    except ValueError:
                        declared_length = None
                if declared_length is not None and declared_length > MAX_RECEIPT_BYTES:
                    raise ReceiptTooLarge(transaction_id, declared_length)

                with destination.open("wb") as handle:
                    try:
                        for chunk in response.iter_bytes(chunk_size=DOWNLOAD_CHUNK_SIZE):
                            if not chunk:
                                continue
                            size += len(chunk)
                            if size > MAX_RECEIPT_BYTES:
                                raise ReceiptTooLarge(transaction_id, size)
                            digest.update(chunk)
                            handle.write(chunk)
                    except (ReceiptFetchFailure,):
                        raise
                    except httpx.HTTPError as exc:
                        # Content-Length 未收满、chunked 编码未正常结束、连接被
                        # 重置等都会落到这里；绝不能把半份内容当作成功。
                        raise StreamInterrupted(
                            transaction_id,
                            f"读取银行源响应流中断: {type(exc).__name__}: {exc}",
                        ) from exc
                    except OSError:
                        raise
                    finally:
                        handle.flush()
                        try:
                            os.fsync(handle.fileno())
                        except OSError:
                            pass

                # 某些实现可能正常结束但实际长度与声明长度不符；再做一次显式核对
                if declared_length is not None and size != declared_length:
                    raise StreamInterrupted(
                        transaction_id,
                        "响应流提前结束：实际字节数与 Content-Length 不一致",
                    )
    except ReceiptFetchFailure:
        raise
    except httpx.TimeoutException as exc:
        raise UpstreamFailure(
            transaction_id, f"访问银行源超时: {type(exc).__name__}: {exc}"
        ) from exc
    except httpx.HTTPError as exc:
        raise UpstreamFailure(
            transaction_id, f"访问银行源失败: {type(exc).__name__}: {exc}"
        ) from exc

    actual = digest.hexdigest()
    if actual != expected:
        raise DigestMismatch(transaction_id, expected, actual, size)

    return FetchResult(sha256=actual, size_bytes=size)
