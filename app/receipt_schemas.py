"""回单采集 / 查询的请求与响应模型。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

MAX_BATCH_ITEMS = 12

# 流水 ID：仅英文字母、数字与短横线；同时天然阻断路径分隔符等注入字符
ID_PATTERN = r"^[A-Za-z0-9-]+$"
SHA256_PATTERN = r"^[0-9a-fA-F]{64}$"

# 流水 ID 的常见同义字段名（提高不同调用方的兼容性）
_ID_ALIASES = AliasChoices(
    "transaction_id",
    "transactionId",
    "id",
    "receipt_id",
    "receiptId",
    "txn_id",
    "txnId",
    "stream_id",
    "streamId",
)

# 预期 SHA-256 的常见同义字段名
_SHA_ALIASES = AliasChoices(
    "sha256",
    "sha_256",
    "expected_sha256",
    "expectedSha256",
    "expected_sha_256",
    "expected_hash",
    "expectedHash",
    "expected_sha",
    "digest",
    "expected_digest",
    "hash",
    "sha",
    "checksum",
)

_ITEMS_KEYS = (
    "items",
    "receipts",
    "requests",
    "batch",
    "receipt_items",
    "entries",
    "transactions",
    "ids",
    "receipt_ids",
)


class ReceiptRequestItem(BaseModel):
    """一项待采集回单：流水 ID 与调用方预期的 SHA-256。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    transaction_id: str = Field(
        ...,
        min_length=1,
        validation_alias=_ID_ALIASES,
        description="流水 ID，仅允许英文字母、数字、短横线，批内唯一",
        examples=["RCPT-DEMO-001"],
    )
    sha256: str = Field(
        ...,
        validation_alias=_SHA_ALIASES,
        description="预期 SHA-256（64 位十六进制，大小写不敏感）",
    )

    @field_validator("transaction_id")
    @classmethod
    def _validate_transaction_id(cls, value: str) -> str:
        if not isinstance(value, str):
            raise ValueError("transaction_id 必须是字符串")
        if not value:
            raise ValueError("transaction_id 不能为空")
        import re

        if re.fullmatch(ID_PATTERN, value) is None:
            raise ValueError(
                "transaction_id 仅允许英文字母(A-Z/a-z)、数字(0-9)和短横线(-)"
            )
        return value

    @field_validator("sha256")
    @classmethod
    def _validate_sha256(cls, value: str) -> str:
        if not isinstance(value, str):
            raise ValueError("sha256 必须是字符串")
        import re

        if re.fullmatch(SHA256_PATTERN, value) is None:
            raise ValueError("sha256 必须是 64 位十六进制字符串")
        return value.lower()


class ReceiptCollectRequest(BaseModel):
    """回单采集批次：1~12 项，批内流水 ID 唯一。"""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "items": [
                        {
                            "transaction_id": "RCPT-DEMO-001",
                            "sha256": (
                                "6b7aa10c704750351c1d4bc9dbee4663"
                                "96644cc7ece1c82244a226eef4366a7a"
                            ),
                        }
                    ]
                }
            ]
        },
    )

    items: list[ReceiptRequestItem] = Field(
        ...,
        min_length=1,
        max_length=MAX_BATCH_ITEMS,
        validation_alias=AliasChoices(*_ITEMS_KEYS),
        description=f"待采集回单列表，{1}~{MAX_BATCH_ITEMS} 项，批内流水 ID 唯一",
    )

    @model_validator(mode="before")
    @classmethod
    def _normalize_shapes(cls, data: Any) -> Any:
        """兼容几种常见的请求体写法。

        * 裸数组 ``[{...}, ...]`` -> ``{"items": [...]}``；
        * ``{"receipts": {"ID": "sha"}}`` 映射 -> items 列表；
        * ``{"transaction_ids": [...], "sha256": [...]}`` 平行数组 -> items 列表。
        非上述形态原样交给 Pydantic 校验，仍会得到严格的 422。
        """
        if isinstance(data, list):
            return {"items": data}
        if not isinstance(data, dict):
            return data

        # 平行数组：transaction_ids / transactionIds / receipt_ids / ids +
        # sha256 / sha256s / expected_sha256 ... -> items 列表
        parallel_ids = None
        for key in ("transaction_ids", "transactionIds", "receipt_ids", "ids"):
            value = data.get(key)
            if isinstance(value, list):
                parallel_ids = value
                break
        parallel_shas = None
        for key in (
            "sha256",
            "sha256s",
            "sha_256",
            "expected_sha256",
            "expected_sha256s",
            "digests",
            "hashes",
        ):
            value = data.get(key)
            if isinstance(value, list):
                parallel_shas = value
                break
        if (
            parallel_ids is not None
            and parallel_shas is not None
            and len(parallel_ids) == len(parallel_shas)
            and all(isinstance(i, str) for i in parallel_ids)
            and all(isinstance(s, str) for s in parallel_shas)
        ):
            return {
                "items": [
                    {"transaction_id": i, "sha256": s}
                    for i, s in zip(parallel_ids, parallel_shas)
                ]
            }

        # 已含列表形态的字段：若其中某个字段是映射，则展开为列表
        for key in _ITEMS_KEYS:
            if key in data:
                value = data[key]
                if isinstance(value, dict):
                    return {"items": _mapping_to_items(value)}
                return data

        # 顶层 ID -> SHA-256 映射
        if data and all(isinstance(v, str) for v in data.values()):
            return {"items": _mapping_to_items(data)}

        return data

    @model_validator(mode="after")
    def _check_unique_ids(self) -> "ReceiptCollectRequest":
        ids = [item.transaction_id for item in self.items]
        if len(set(ids)) != len(ids):
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(f"批内 transaction_id 重复: {dupes}")
        return self


def _mapping_to_items(mapping: dict) -> list[dict]:
    return [
        {"transaction_id": key, "sha256": value}
        for key, value in mapping.items()
    ]


class CollectedReceipt(BaseModel):
    transaction_id: str
    sha256: str
    size_bytes: int
    reused: bool = Field(..., description="true 表示本批复用了已归档原件，未重新下载")
    download_url: str


class ReceiptCollectResponse(BaseModel):
    status: Literal["published"] = "published"
    batch_id: str
    total: int = Field(..., description="本批项目数（1~12）")
    published: int = Field(..., description="本批新发布的回单数量")
    reused: int = Field(..., description="本批复用的已归档回单数量")
    receipts: list[CollectedReceipt] = Field(
        ..., description="按请求顺序排列的本批回单；整批要么全部发布，要么全部不发布"
    )


class ReceiptMetadata(BaseModel):
    transaction_id: str
    sha256: str
    size_bytes: int
    created_at: str
    download_url: str


class ReceiptListResponse(BaseModel):
    total: int
    receipts: list[ReceiptMetadata]
