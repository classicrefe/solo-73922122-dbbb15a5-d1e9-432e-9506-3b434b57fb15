"""FastAPI 应用入口：支付渠道日结对账服务。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from .schemas import (
    MAX_DATE_DELTA_DAYS,
    MAX_RECORDS,
    MAX_SETTLEMENTS_PER_ENTRY,
    ReconcileRequest,
    ReconcileResponse,
)
from .solver import solve

app = FastAPI(
    title="支付渠道日结对账 API",
    description=(
        "核对外渠道日结单（扣费后应结金额）与银行贷记流水，求解全局匹配入账总"
        "金额最大的方案，并按「所有最优方案中的一致性」区分自动确认、人工核对"
        "与始终未匹配记录。\n\n"
        "## 匹配规则\n"
        f"* 一笔银行入账可匹配 1~{MAX_SETTLEMENTS_PER_ENTRY} 条**同币种**日结记录；\n"
        "* 日结合计金额（正整数分）必须与入账金额**严格相等**；\n"
        f"* 入账日须在每条结算日之后 0~{MAX_DATE_DELTA_DAYS} 个自然日内（含端点）；\n"
        "* 每条记录在一套方案中只能使用一次；\n"
        f"* 日结记录与银行流水各不超过 {MAX_RECORDS} 条。\n\n"
        "## 结果口径\n"
        "* `auto_confirm`：在**所有**最优方案中均对应同一日结组的入账；\n"
        "* `manual_review`：不同最优方案中对应不同日结组的入账（歧义）；\n"
        "* `never_matched`：在任何最优方案中始终未被匹配的记录；\n"
        "* 存在歧义时 `solutions` 返回两套不同的完整最优方案。"
    ),
    version="1.0.0",
)


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/health", tags=["运维"], summary="健康检查")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/api/v1/reconcile",
    response_model=ReconcileResponse,
    tags=["对账"],
    summary="求解全局最优对账匹配",
    responses={
        422: {"description": "输入校验失败（编号重复、金额非正整数、超出条数等）"}
    },
)
def reconcile(payload: ReconcileRequest) -> ReconcileResponse:
    result = solve(payload.settlements, payload.bank_entries)
    return ReconcileResponse(**result)
