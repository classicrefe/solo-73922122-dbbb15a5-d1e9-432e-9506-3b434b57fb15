"""请求 / 响应数据模型与输入校验。"""

from __future__ import annotations

from datetime import date
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# 单组记录上限（日结 / 银行流水各不超过 12 条）
MAX_RECORDS = 12
# 一笔入账最多可合并匹配的日结记录数
MAX_SETTLEMENTS_PER_ENTRY = 3
# 入账日相对结算日允许的最大自然日跨度
MAX_DATE_DELTA_DAYS = 3

# 正整数分金额：严格大于 0
PositiveCents = Annotated[int, Field(strict=True, gt=0)]
# 编号：非空字符串，去除首尾空白后仍需非空
RecordId = Annotated[str, Field(min_length=1, max_length=64)]


class _BaseRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: RecordId
    date: date
    currency: Annotated[str, Field(min_length=3, max_length=3)]
    amount_cents: PositiveCents

    @field_validator("id", "currency")
    @classmethod
    def _strip_and_upper(cls, value: str, info) -> str:
        value = value.strip()
        if not value:
            raise ValueError(f"{info.field_name} 不能为空字符串")
        if info.field_name == "currency":
            value = value.upper()
        return value


class SettlementRecord(_BaseRecord):
    """日结记录。amount_cents 为扣费后应结金额（正整数分）。"""


class BankEntry(_BaseRecord):
    """银行贷记流水（入账）。amount_cents 为实际入账金额（正整数分）。"""


class ReconcileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    settlements: list[SettlementRecord] = Field(
        ...,
        description="日结记录列表，数量 0~12，组内 id 唯一",
        max_length=MAX_RECORDS,
    )
    bank_entries: list[BankEntry] = Field(
        ...,
        alias="bank_entries",
        description="银行贷记流水列表，数量 0~12，组内 id 唯一",
        max_length=MAX_RECORDS,
    )

    @model_validator(mode="after")
    def _check_unique_ids(self) -> "ReconcileRequest":
        for field_name in ("settlements", "bank_entries"):
            records = getattr(self, field_name)
            ids = [r.id for r in records]
            if len(set(ids)) != len(ids):
                dupes = sorted({i for i in ids if ids.count(i) > 1})
                raise ValueError(f"{field_name} 中存在重复编号: {dupes}")
        return self


class Match(BaseModel):
    """一条入账与一组日结记录的匹配关系。"""

    bank_entry_id: str
    settlement_ids: list[str]
    currency: str
    amount_cents: int


class Solution(BaseModel):
    """一套完整的匹配方案。"""

    total_matched_cents: int = Field(..., description="本方案已匹配入账总金额（分）")
    matches: list[Match]
    unmatched_settlement_ids: list[str]
    unmatched_bank_entry_ids: list[str]


class AutoConfirmation(BaseModel):
    """在所有最优方案中都对应同一日结组的入账，可自动确认。"""

    bank_entry_id: str
    settlement_ids: list[str]
    currency: str
    amount_cents: int


class ManualReviewItem(BaseModel):
    """存在多种最优匹配方式、需要人工核对的入账。"""

    bank_entry_id: str
    currency: str
    amount_cents: int
    candidate_settlement_groups: list[list[str]] = Field(
        ..., description="该入账在全部最优方案中出现过的不同日结组"
    )


class ReconcileResponse(BaseModel):
    optimal_total_matched_cents: int = Field(
        ..., description="全局最优方案的已匹配入账总金额（分）"
    )
    total_optimal_solutions: int = Field(
        ..., description="枚举到的不同最优方案数量（上限封顶为 2）"
    )
    is_unique: bool
    solutions: list[Solution] = Field(
        ..., max_length=2, description="最优方案；唯一时 1 套，有歧义时 2 套不同方案"
    )
    auto_confirm: list[AutoConfirmation] = Field(
        ..., description="所有最优方案中均对应同一日结组的入账"
    )
    manual_review: list[ManualReviewItem] = Field(
        ..., description="在不同最优方案中匹配不同日结组的入账"
    )
    never_matched: dict[str, list[str]] = Field(
        ..., description="始终未匹配的记录编号: {settlements, bank_entries}"
    )
