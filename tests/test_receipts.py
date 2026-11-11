"""回单采集 / 原子发布 / 并发 / 持久化集成测试。"""

from __future__ import annotations

import base64
import concurrent.futures
import hashlib
import socket
import sqlite3
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from app.main import app
from app.receipt_schemas import ReceiptCollectRequest
from app.receipts import _collect
from bank_source.main import app as bank_app


DEMO_1 = "RCPT-DEMO-001"
DEMO_2 = "RCPT-DEMO-002"
DEMO_BAD = "RCPT-DEMO-BAD"
DEMO_TRUNC = "RCPT-DEMO-TRUNC"
DEMO_BIG = "RCPT-DEMO-BIG"

SHA_1 = "6b7aa10c704750351c1d4bc9dbee466396644cc7ece1c82244a226eef4366a7a"
SHA_2 = "075383616cdc9c330873460aeb0db278fbd3d03a70154968b5a84906390dfa44"
SHA_BAD_INTENDED = (
    "46aaf6a1078682006f193dd7fbc57fa73f685775e10c5137b28195487b8a4b66"
)
SHA_TRUNC_INTENDED = (
    "fadb70ba80ba9fe694cf34453495d02da432f9adbcc724ed90d21d651bc4daa9"
)


@pytest.fixture(scope="session")
def bank_source_url() -> str:
    """在后台线程启动本地银行源（真实 HTTP，支持断流/坏摘要/超限）。"""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    config = uvicorn.Config(
        bank_app,
        host="127.0.0.1",
        port=port,
        log_level="error",
        access_log=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            if httpx.get(f"{url}/health", timeout=0.5).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.05)
    else:  # pragma: no cover
        raise RuntimeError("bank source failed to start")

    yield url

    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture()
def api(bank_source_url, tmp_path, monkeypatch):
    monkeypatch.setenv("RECEIPT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("BANK_RECEIPT_BASE_URL", bank_source_url)
    # 每个用例恢复银行源默认数据
    httpx.post(f"{bank_source_url}/admin/reset", timeout=5).raise_for_status()
    with TestClient(app) as client:
        yield client


def _item(tid: str, sha: str) -> dict:
    return {"transaction_id": tid, "sha256": sha}


def _put_receipt(bank_source_url: str, tid: str, content: bytes, mode: str = "normal"):
    resp = httpx.put(
        f"{bank_source_url}/admin/receipts/{tid}",
        json={
            "content_base64": base64.b64encode(content).decode(),
            "mode": mode,
        },
        timeout=5,
    )
    assert resp.status_code == 200, resp.text
    return hashlib.sha256(content).hexdigest()


# --------------------------------------------------------------------------
# 正常采集 / 下载 / 复用 / 重启保留
# --------------------------------------------------------------------------


def test_collect_download_metadata_and_restart_persistence(api, tmp_path, monkeypatch):
    resp = api.post(
        "/api/v1/receipts/collect", json={"items": [_item(DEMO_1, SHA_1)]}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "published"
    assert body["total"] == 1 and body["published"] == 1 and body["reused"] == 0
    assert body["receipts"][0]["download_url"] == f"/api/v1/receipts/{DEMO_1}"

    download = api.get(f"/api/v1/receipts/{DEMO_1}")
    assert download.status_code == 200
    assert download.content.startswith(b"BANK RECEIPT")
    assert download.headers["x-receipt-sha256"] == SHA_1
    assert f"{DEMO_1}.bin" in download.headers["content-disposition"]

    meta = api.get(f"/api/v1/receipts/{DEMO_1}/metadata")
    assert meta.status_code == 200
    assert meta.json()["sha256"] == SHA_1
    assert meta.json()["size_bytes"] == len(download.content)

    # 新 TestClient（新进程语义）模拟服务重启；环境变量指向同一数据目录
    with TestClient(app) as restarted:
        again = restarted.get(f"/api/v1/receipts/{DEMO_1}")
        assert again.status_code == 200
        assert again.content == download.content


def test_same_id_same_digest_reuses_without_refetch(api, bank_source_url):
    httpx.post(f"{bank_source_url}/admin/reset", timeout=5)
    r1 = api.post(
        "/api/v1/receipts/collect", json={"items": [_item(DEMO_1, SHA_1)]}
    )
    assert r1.status_code == 200
    stats_after_first = httpx.get(
        f"{bank_source_url}/admin/stats", timeout=5
    ).json()["receipt_gets"].get(DEMO_1, 0)
    assert stats_after_first == 1

    r2 = api.post(
        "/api/v1/receipts/collect", json={"items": [_item(DEMO_1, SHA_1)]}
    )
    assert r2.status_code == 200
    assert r2.json()["reused"] == 1 and r2.json()["published"] == 0
    stats_after_second = httpx.get(
        f"{bank_source_url}/admin/stats", timeout=5
    ).json()["receipt_gets"].get(DEMO_1, 0)
    assert stats_after_second == 1, "同 ID 同摘要应复用，不应再次访问银行源"


def test_alias_shapes_accepted(api):
    # 裸数组 + id/expected_sha256 同义字段
    resp = api.post(
        "/api/v1/receipts/collect",
        json=[{"id": DEMO_1, "expected_sha256": SHA_1.upper()}],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["published"] == 1

    # 顶层 ID -> SHA 映射；已归档同摘要应复用
    resp2 = api.post("/api/v1/receipts/collect", json={DEMO_1: SHA_1})
    assert resp2.status_code == 200, resp2.text
    assert resp2.json()["reused"] == 1


# --------------------------------------------------------------------------
# 整批原子性：断流 / 超限 / 摘要错误
# --------------------------------------------------------------------------


def test_digest_mismatch_rejects_whole_batch(api):
    resp = api.post(
        "/api/v1/receipts/collect",
        json={
            "items": [
                _item(DEMO_2, SHA_2),  # 本身合法
                _item(DEMO_BAD, SHA_BAD_INTENDED),  # 源端篡改内容
            ]
        },
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "digest_mismatch"
    assert resp.json()["detail"]["batch_rejected"] is True

    # 整批都不会暴露，包括本来成功的那一项
    assert api.get(f"/api/v1/receipts/{DEMO_2}").status_code == 404
    assert api.get(f"/api/v1/receipts/{DEMO_BAD}").status_code == 404
    assert api.get("/api/v1/receipts").json()["total"] == 0


def test_truncated_stream_rejects_whole_batch(api):
    resp = api.post(
        "/api/v1/receipts/collect",
        json={"items": [_item(DEMO_TRUNC, SHA_TRUNC_INTENDED)]},
    )
    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "stream_interrupted"
    assert api.get(f"/api/v1/receipts/{DEMO_TRUNC}").status_code == 404
    assert api.get("/api/v1/receipts").json()["total"] == 0


def test_oversize_rejects_whole_batch(api):
    resp = api.post(
        "/api/v1/receipts/collect",
        json={"items": [_item(DEMO_BIG, "0" * 64)]},
    )
    assert resp.status_code == 413
    assert resp.json()["detail"]["code"] == "receipt_too_large"
    assert api.get(f"/api/v1/receipts/{DEMO_BIG}").status_code == 404
    assert api.get("/api/v1/receipts").json()["total"] == 0


# --------------------------------------------------------------------------
# 同 ID 异摘要 / 失败清理安全性
# --------------------------------------------------------------------------


def test_different_digest_rejects_entire_batch(api, bank_source_url):
    new_id = "RCPT-TEST-NEW"
    new_sha = _put_receipt(bank_source_url, new_id, b"brand-new-content")

    # 先正常归档 DEMO_1
    assert (
        api.post(
            "/api/v1/receipts/collect", json={"items": [_item(DEMO_1, SHA_1)]}
        ).status_code
        == 200
    )

    # 批内第一项同 ID 异摘要 -> 409，整批拒绝；第二项新回单不得发布
    resp = api.post(
        "/api/v1/receipts/collect",
        json={"items": [_item(DEMO_1, "1" * 64), _item(new_id, new_sha)]},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "receipt_conflict"
    assert resp.json()["detail"]["batch_rejected"] is True
    assert api.get(f"/api/v1/receipts/{new_id}").status_code == 404

    # 已发布的原件必须仍然可下载
    assert api.get(f"/api/v1/receipts/{DEMO_1}").status_code == 200
    assert api.get(f"/api/v1/receipts/{DEMO_1}").content.startswith(b"BANK RECEIPT")


def test_failed_batch_cleanup_keeps_other_published_receipt(api):
    # 先归档 DEMO_1
    assert (
        api.post(
            "/api/v1/receipts/collect", json={"items": [_item(DEMO_1, SHA_1)]}
        ).status_code
        == 200
    )
    # 第二批包含已归档项（应复用）+ 坏摘要项（整批失败）
    resp = api.post(
        "/api/v1/receipts/collect",
        json={"items": [_item(DEMO_1, SHA_1), _item(DEMO_BAD, SHA_BAD_INTENDED)]},
    )
    assert resp.status_code == 422

    # 失败批次的清理不得删除先前批次已发布的共享原件
    still = api.get(f"/api/v1/receipts/{DEMO_1}")
    assert still.status_code == 200
    assert still.content.startswith(b"BANK RECEIPT")


# --------------------------------------------------------------------------
# 请求校验
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"items": []},
        {"items": [{"transaction_id": f"R{i}", "sha256": "a" * 64} for i in range(13)]},
        {
            "items": [
                {"transaction_id": "DUP", "sha256": "a" * 64},
                {"transaction_id": "DUP", "sha256": "b" * 64},
            ]
        },
        {"items": [{"transaction_id": "../escape", "sha256": "a" * 64}]},
        {"items": [{"transaction_id": "bad_id", "sha256": "a" * 64}]},
        {"items": [{"transaction_id": "含中文", "sha256": "a" * 64}]},
        {"items": [{"transaction_id": "OK-1", "sha256": "xyz"}]},
        {"items": [{"transaction_id": "OK-1", "sha256": "g" * 64}]},
        {"items": [{"transaction_id": "OK-1"}]},
    ],
)
def test_invalid_requests_rejected(api, payload):
    resp = api.post("/api/v1/receipts/collect", json=payload)
    assert resp.status_code == 422
    assert api.get("/api/v1/receipts").json()["total"] == 0


def test_sqlite_stores_index_only(api, tmp_path):
    assert (
        api.post(
            "/api/v1/receipts/collect", json={"items": [_item(DEMO_1, SHA_1)]}
        ).status_code
        == 200
    )
    db = tmp_path / "data" / "receipts.db"
    conn = sqlite3.connect(db)
    try:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(receipts)")]
        rows = conn.execute("SELECT * FROM receipts").fetchall()
    finally:
        conn.close()
    assert columns == [
        "transaction_id",
        "sha256",
        "size_bytes",
        "created_at",
    ]
    assert len(rows) == 1
    # 原件字节在文件系统中，不在 SQLite
    obj = tmp_path / "data" / "objects" / SHA_1
    assert obj.is_file()
    assert obj.read_bytes().startswith(b"BANK RECEIPT")


# --------------------------------------------------------------------------
# 并发：同一进程内多线程；跨进程场景由 Docker Compose 两个服务覆盖
# --------------------------------------------------------------------------


def test_concurrent_overlapping_batches_same_digest(api, bank_source_url):
    new_id = "RCPT-CONC-NEW"
    new_sha = _put_receipt(bank_source_url, new_id, b"concurrent-extra-content")

    # 使用与 TestClient 相同的数据目录，直接在线程中执行核心采集逻辑
    # （等价于两个 API 进程各自处理一个重叠批次）。

    payloads = [
        ReceiptCollectRequest.model_validate(
            {
                "items": [
                    {"transaction_id": DEMO_1, "sha256": SHA_1},
                    {"transaction_id": new_id, "sha256": new_sha},
                ]
            }
        ),
        ReceiptCollectRequest.model_validate(
            {
                "items": [
                    {"transaction_id": DEMO_1, "sha256": SHA_1},
                    {"transaction_id": new_id, "sha256": new_sha},
                ]
            }
        ),
    ]

    def run(payload):
        return _collect(payload)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, payloads))

    assert all(r.total == 2 for r in results)
    # 两项都只发布一次：两批合计 published + reused == 4，且 published == 2
    assert sum(r.published for r in results) == 2
    assert sum(r.reused for r in results) == 2

    listing = api.get("/api/v1/receipts").json()
    assert listing["total"] == 2
    ids = {row["transaction_id"] for row in listing["receipts"]}
    assert ids == {DEMO_1, new_id}
    assert api.get(f"/api/v1/receipts/{DEMO_1}").status_code == 200
    assert api.get(f"/api/v1/receipts/{new_id}").status_code == 200


def test_slow_download_does_not_block_other_publish(api, bank_source_url):
    """网络等待期间不持有写锁：慢速下载期间另一批次可完成发布。"""
    slow_id = "RCPT-TEST-SLOW"
    slow_content = b"slow-receipt-" + b"A" * 200
    slow_sha = _put_receipt(bank_source_url, slow_id, slow_content, mode="slow")
    fast_id = "RCPT-TEST-FAST"
    fast_sha = _put_receipt(bank_source_url, fast_id, b"fast-content")

    import os

    def collect_slow():
        return _collect(
            ReceiptCollectRequest.model_validate(
                {"items": [{"transaction_id": slow_id, "sha256": slow_sha}]}
            )
        )

    def collect_fast():
        # 给慢请求一点时间先进入流式读取
        time.sleep(0.2)
        started = time.time()
        result = _collect(
            ReceiptCollectRequest.model_validate(
                {"items": [{"transaction_id": fast_id, "sha256": fast_sha}]}
            )
        )
        return result, time.time() - started

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        slow_future = pool.submit(collect_slow)
        fast_result, fast_elapsed = collect_fast()
        slow_result = slow_future.result(timeout=30)

    assert slow_result.published == 1
    assert fast_result.published == 1
    # 快速批次不应被慢速下载长时间阻塞（允许少量调度/写事务开销）
    assert fast_elapsed < 3.0
    assert api.get(f"/api/v1/receipts/{fast_id}").status_code == 200
    assert api.get(f"/api/v1/receipts/{slow_id}").status_code == 200
