"""FastAPI 应用入口：支付渠道日结对账 + 银行回单采集服务。"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from .config import MAX_RECEIPT_BYTES
from .receipt_store import ensure_initialized
from .receipts import router as receipts_router
from .schemas import (
    MAX_DATE_DELTA_DAYS,
    MAX_RECORDS,
    MAX_SETTLEMENTS_PER_ENTRY,
    ReconcileRequest,
    ReconcileResponse,
)
from .solver import solve


@asynccontextmanager
async def lifespan(_: FastAPI):
    # 创建数据目录与 SQLite 索引表；两个 API 进程可并发执行，幂等。
    ensure_initialized()
    yield


app = FastAPI(
    title="支付渠道对账与银行回单采集 API",
    description=(
        "本服务包含两个相互独立的业务入口：\n\n"
        "1. **日结对账**：核对外渠道日结单与银行贷记流水，求解全局最优匹配；\n"
        "2. **回单采集**：按流水 ID 从配置固定的银行 HTTP 源逐块下载回单原件，"
        "校验 SHA-256 后原子发布，并提供按流水 ID 下载完整原件的能力。\n\n"
        "## 回单采集规则\n"
        f"* 每批 1~12 项，流水 ID 仅允许英文字母、数字、短横线，批内唯一；\n"
        f"* 银行源地址由环境变量 `BANK_RECEIPT_BASE_URL` 固定配置；\n"
        f"* 单份回单最多 16 MiB（{MAX_RECEIPT_BYTES} 字节），逐块下载；\n"
        "* 全部回单下载并核验成功后才**一次性发布整批**；断流、超限、摘要错误"
        "或进程中断都不会暴露半批；\n"
        "* 同 ID 同摘要复用已归档原件；同 ID 异摘要拒绝整批；\n"
        "* 原件保存为本地文件，SQLite 只保存索引，服务重启后仍可查询下载。\n\n"
        "## 在 /docs 中演示采集与下载\n"
        "1. 打开本地银行源文档 `http://localhost:9000/docs`，或请求 "
        "`GET http://localhost:9000/receipts` 查看演示回单及预期 SHA-256；\n"
        "2. 执行 `POST /api/v1/receipts/collect`（请求体示例已内置，"
        "可直接使用 `RCPT-DEMO-001`）；\n"
        "3. 执行 `GET /api/v1/receipts/RCPT-DEMO-001` 下载刚归档的完整原件；\n"
        "4. 以 `RCPT-DEMO-TRUNC` / `RCPT-DEMO-BAD` / `RCPT-DEMO-BIG` 可分别演示"
        "断流、坏摘要、超限导致的整批回滚。\n\n"
        "## 对账匹配规则\n"
        f"* 一笔银行入账可匹配 1~{MAX_SETTLEMENTS_PER_ENTRY} 条**同币种**日结记录；\n"
        "* 日结合计金额（正整数分）必须与入账金额**严格相等**；\n"
        f"* 入账日须在每条结算日之后 0~{MAX_DATE_DELTA_DAYS} 个自然日内（含端点）；\n"
        "* 每条记录在一套方案中只能使用一次；\n"
        f"* 日结记录与银行流水各不超过 {MAX_RECORDS} 条。\n\n"
        "## 对账结果口径\n"
        "* `auto_confirm`：在**所有**最优方案中均对应同一日结组的入账；\n"
        "* `manual_review`：不同最优方案中对应不同日结组的入账（歧义）；\n"
        "* `never_matched`：在任何最优方案中始终未被匹配的记录；\n"
        "* 存在歧义时 `solutions` 返回两套不同的完整最优方案。"
    ),
    version="2.0.0",
    lifespan=lifespan,
)

# 保留原有核对入口，新增回单采集/下载路由
app.include_router(receipts_router)


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
