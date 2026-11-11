"""求解器与 API 的单元 / 集成测试。"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.schemas import ReconcileRequest
from app.solver import solve

client = TestClient(app)


def _sid(i: int) -> str:
    return f"S{i:03d}"


def _bid(i: int) -> str:
    return f"B{i:03d}"


def _settlement(i: int, amount: int, day: date, currency: str = "CNY", sid=None):
    return {
        "id": sid or _sid(i),
        "date": day.isoformat(),
        "currency": currency,
        "amount_cents": amount,
    }


def _bank(i: int, amount: int, day: date, currency: str = "CNY", bid=None):
    return {
        "id": bid or _bid(i),
        "date": day.isoformat(),
        "currency": currency,
        "amount_cents": amount,
    }


# ---------------------------------------------------------------- 基础匹配


def test_single_one_to_one_match():
    d = date(2026, 11, 1)
    req = ReconcileRequest.model_validate(
        {
            "settlements": [_settlement(1, 1000, d)],
            "bank_entries": [_bank(1, 1000, date(2026, 11, 2))],
        }
    )
    res = solve(req.settlements, req.bank_entries)
    assert res["optimal_total_matched_cents"] == 1000
    assert res["is_unique"] is True
    assert len(res["solutions"]) == 1
    assert res["auto_confirm"][0]["bank_entry_id"] == "B001"
    assert res["auto_confirm"][0]["settlement_ids"] == ["S001"]
    assert res["never_matched"] == {"settlements": [], "bank_entries": []}


def test_merged_payment_two_and_three():
    d = date(2026, 11, 1)
    payload = {
        "settlements": [
            _settlement(1, 100, d),
            _settlement(2, 200, d),
            _settlement(3, 300, d),
        ],
        "bank_entries": [_bank(1, 600, date(2026, 11, 2))],
    }
    req = ReconcileRequest.model_validate(payload)
    res = solve(req.settlements, req.bank_entries)
    assert res["optimal_total_matched_cents"] == 600
    group = res["auto_confirm"][0]["settlement_ids"]
    assert sorted(group) == ["S001", "S002", "S003"]


def test_group_size_limit_is_three():
    # 四条 100 分日结合计 400，不允许 4 条合并 -> 400 入账无法匹配
    d = date(2026, 11, 1)
    payload = {
        "settlements": [_settlement(i, 100, d) for i in range(1, 5)],
        "bank_entries": [_bank(1, 400, date(2026, 11, 2))],
    }
    req = ReconcileRequest.model_validate(payload)
    res = solve(req.settlements, req.bank_entries)
    assert res["optimal_total_matched_cents"] == 0
    assert res["never_matched"]["bank_entries"] == ["B001"]
    assert sorted(res["never_matched"]["settlements"]) == [
        "S001",
        "S002",
        "S003",
        "S004",
    ]


# ---------------------------------------------------------------- 日期窗口


@pytest.mark.parametrize(
    "bank_day,expected_matched",
    [
        (date(2026, 11, 1), 1000),  # delta 0，同日
        (date(2026, 11, 4), 1000),  # delta 3，边界含
        (date(2026, 11, 5), 0),     # delta 4，超窗
        (date(2026, 10, 31), 0),    # 入账早于结算日
    ],
)
def test_date_window_boundaries(bank_day, expected_matched):
    payload = {
        "settlements": [_settlement(1, 1000, date(2026, 11, 1))],
        "bank_entries": [_bank(1, 1000, bank_day)],
    }
    req = ReconcileRequest.model_validate(payload)
    res = solve(req.settlements, req.bank_entries)
    assert res["optimal_total_matched_cents"] == expected_matched


# ---------------------------------------------------------------- 币种 / 金额


def test_currency_mismatch_blocks_match():
    payload = {
        "settlements": [_settlement(1, 1000, date(2026, 11, 1), "CNY")],
        "bank_entries": [_bank(1, 1000, date(2026, 11, 2), "USD")],
    }
    req = ReconcileRequest.model_validate(payload)
    res = solve(req.settlements, req.bank_entries)
    assert res["optimal_total_matched_cents"] == 0
    assert res["never_matched"]["bank_entries"] == ["B001"]


def test_amount_mismatch_blocks_match():
    payload = {
        "settlements": [_settlement(1, 999, date(2026, 11, 1))],
        "bank_entries": [_bank(1, 1000, date(2026, 11, 2))],
    }
    req = ReconcileRequest.model_validate(payload)
    res = solve(req.settlements, req.bank_entries)
    assert res["optimal_total_matched_cents"] == 0


def test_currency_code_case_insensitive():
    payload = {
        "settlements": [_settlement(1, 1000, date(2026, 11, 1), "cny")],
        "bank_entries": [_bank(1, 1000, date(2026, 11, 2), "cnY")],
    }
    req = ReconcileRequest.model_validate(payload)
    res = solve(req.settlements, req.bank_entries)
    assert res["optimal_total_matched_cents"] == 1000


# ---------------------------------------------------------------- 最优性


def test_optimum_prefers_larger_total():
    d = date(2026, 11, 1)
    payload = {
        "settlements": [_settlement(1, 100, d), _settlement(2, 100, d)],
        "bank_entries": [
            _bank(1, 100, date(2026, 11, 2)),
            _bank(2, 200, date(2026, 11, 2)),
        ],
    }
    req = ReconcileRequest.model_validate(payload)
    res = solve(req.settlements, req.bank_entries)
    # 合并打款 200 优于单笔 100
    assert res["optimal_total_matched_cents"] == 200
    assert res["auto_confirm"][0]["bank_entry_id"] == "B002"
    assert sorted(res["auto_confirm"][0]["settlement_ids"]) == ["S001", "S002"]
    assert res["never_matched"]["bank_entries"] == ["B001"]


# ---------------------------------------------------------------- 歧义


def test_ambiguous_swap_returns_two_solutions():
    d = date(2026, 11, 1)
    payload = {
        "settlements": [
            _settlement(1, 100, d),
            _settlement(2, 200, d),
            _settlement(3, 100, d),
            _settlement(4, 200, d),
        ],
        "bank_entries": [
            _bank(1, 300, date(2026, 11, 2)),
            _bank(2, 300, date(2026, 11, 2)),
        ],
    }
    req = ReconcileRequest.model_validate(payload)
    res = solve(req.settlements, req.bank_entries)

    assert res["optimal_total_matched_cents"] == 600
    assert res["is_unique"] is False
    assert res["total_optimal_solutions"] == 2
    assert len(res["solutions"]) == 2

    # 两套方案必须不同，且各自完整合法
    group_sets = []
    for sol in res["solutions"]:
        assert sol["total_matched_cents"] == 600
        used = []
        for m in sol["matches"]:
            used.extend(m["settlement_ids"])
        assert len(used) == len(set(used)) == 4
        group_sets.append(
            sorted((m["bank_entry_id"], tuple(sorted(m["settlement_ids"]))) for m in sol["matches"])
        )
    assert group_sets[0] != group_sets[1]

    # 两条入账都有歧义，不能自动确认
    assert res["auto_confirm"] == []
    assert {item["bank_entry_id"] for item in res["manual_review"]} == {"B001", "B002"}
    # 金额 300 的入账可组成 4 个合法日结组，且全部能进入最优方案
    for item in res["manual_review"]:
        assert len(item["candidate_settlement_groups"]) == 4
        for group in item["candidate_settlement_groups"]:
            assert len(group) <= 3
    assert res["never_matched"] == {"settlements": [], "bank_entries": []}


def test_auto_confirm_when_multiple_solutions_but_bank_mapping_stable():
    # b1 的日结组在所有最优方案中唯一；b2/b3 互换产生两套方案
    d = date(2026, 11, 1)
    payload = {
        "settlements": [
            _settlement(1, 99, d),
            _settlement(2, 102, d),
            _settlement(3, 33, d),
            _settlement(4, 37, d),
            _settlement(5, 100, d),
            _settlement(6, 100, d),
        ],
        "bank_entries": [
            _bank(1, 201, date(2026, 11, 2)),
            _bank(2, 100, date(2026, 11, 2)),
            _bank(3, 100, date(2026, 11, 2)),
        ],
    }
    req = ReconcileRequest.model_validate(payload)
    res = solve(req.settlements, req.bank_entries)

    assert res["optimal_total_matched_cents"] == 401
    assert res["is_unique"] is False
    assert len(res["solutions"]) == 2

    auto = {item["bank_entry_id"]: item["settlement_ids"] for item in res["auto_confirm"]}
    assert auto == {"B001": ["S001", "S002"]}
    assert {item["bank_entry_id"] for item in res["manual_review"]} == {"B002", "B003"}
    # 无用记录（33+37 凑不出任何入账金额）在任何最优方案中都不出现
    assert sorted(res["never_matched"]["settlements"]) == ["S003", "S004"]


def test_records_used_at_most_once_across_solution():
    # 两条入账都只能匹配同一条日结 -> 只能选一条，最优 500
    payload = {
        "settlements": [_settlement(1, 500, date(2026, 11, 1))],
        "bank_entries": [
            _bank(1, 500, date(2026, 11, 2)),
            _bank(2, 500, date(2026, 11, 3)),
        ],
    }
    req = ReconcileRequest.model_validate(payload)
    res = solve(req.settlements, req.bank_entries)
    assert res["optimal_total_matched_cents"] == 500
    # 两套最优方案各只含 1 条匹配（同一条日结不能被两条入账重复使用）
    assert all(len(sol["matches"]) == 1 for sol in res["solutions"])
    # 每条入账的日结组都唯一确定（若被选用则匹配 S001），故均可自动确认；
    # 整体方案二选一的差异体现在 solutions 中，而不是入账-日结映射歧义
    assert {item["bank_entry_id"] for item in res["auto_confirm"]} == {"B001", "B002"}
    assert res["manual_review"] == []
    assert res["never_matched"] == {"settlements": [], "bank_entries": []}


# ---------------------------------------------------------------- 空输入 / 边界


def test_empty_inputs():
    req = ReconcileRequest.model_validate({"settlements": [], "bank_entries": []})
    res = solve(req.settlements, req.bank_entries)
    assert res["optimal_total_matched_cents"] == 0
    assert res["is_unique"] is True
    assert res["solutions"][0]["matches"] == []


def test_max_twelve_records_runs():
    d = date(2026, 11, 1)
    payload = {
        "settlements": [_settlement(i, 100, d) for i in range(1, 13)],
        "bank_entries": [
            _bank(i, 300, date(2026, 11, 2)) for i in range(1, 5)
        ],
    }
    req = ReconcileRequest.model_validate(payload)
    res = solve(req.settlements, req.bank_entries)
    # 4 条入账各合并 3 条日结，恰好用完 12 条
    assert res["optimal_total_matched_cents"] == 1200
    assert res["never_matched"] == {"settlements": [], "bank_entries": []}


# ---------------------------------------------------------------- 输入校验


def _validation_payload(**overrides):
    payload = {
        "settlements": [_settlement(1, 1000, date(2026, 11, 1))],
        "bank_entries": [_bank(1, 1000, date(2026, 11, 2))],
    }
    payload.update(overrides)
    return payload


def test_duplicate_settlement_id_rejected():
    payload = _validation_payload(
        settlements=[
            _settlement(1, 100, date(2026, 11, 1), sid="DUP"),
            _settlement(2, 200, date(2026, 11, 1), sid="DUP"),
        ]
    )
    with pytest.raises(Exception):
        ReconcileRequest.model_validate(payload)


def test_more_than_twelve_records_rejected():
    payload = _validation_payload(
        settlements=[_settlement(i, 100, date(2026, 11, 1)) for i in range(1, 14)]
    )
    with pytest.raises(Exception):
        ReconcileRequest.model_validate(payload)


@pytest.mark.parametrize("amount", [0, -100])
def test_non_positive_amount_rejected(amount):
    payload = _validation_payload(
        settlements=[_settlement(1, amount, date(2026, 11, 1))]
    )
    with pytest.raises(Exception):
        ReconcileRequest.model_validate(payload)


def test_unknown_field_rejected():
    payload = _validation_payload()
    payload["settlements"][0]["unexpected"] = 1
    with pytest.raises(Exception):
        ReconcileRequest.model_validate(payload)


def test_bad_currency_length_rejected():
    payload = _validation_payload(
        settlements=[_settlement(1, 100, date(2026, 11, 1), currency="CN")]
    )
    with pytest.raises(Exception):
        ReconcileRequest.model_validate(payload)


# ---------------------------------------------------------------- HTTP 集成


def test_health_endpoint():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_reconcile_endpoint_unique():
    payload = {
        "settlements": [
            {
                "id": "S001",
                "date": "2026-11-01",
                "currency": "CNY",
                "amount_cents": 1000,
            }
        ],
        "bank_entries": [
            {
                "id": "B001",
                "date": "2026-11-02",
                "currency": "CNY",
                "amount_cents": 1000,
            }
        ],
    }
    resp = client.post("/api/v1/reconcile", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["optimal_total_matched_cents"] == 1000
    assert body["is_unique"] is True
    assert body["auto_confirm"][0]["bank_entry_id"] == "B001"


def test_reconcile_endpoint_ambiguous():
    d = "2026-11-01"
    payload = {
        "settlements": [
            {"id": f"S{i}", "date": d, "currency": "CNY", "amount_cents": a}
            for i, a in enumerate([100, 200, 100, 200], start=1)
        ],
        "bank_entries": [
            {"id": "B1", "date": "2026-11-02", "currency": "CNY", "amount_cents": 300},
            {"id": "B2", "date": "2026-11-02", "currency": "CNY", "amount_cents": 300},
        ],
    }
    resp = client.post("/api/v1/reconcile", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_unique"] is False
    assert len(body["solutions"]) == 2
    assert len(body["manual_review"]) == 2


def test_http_validation_error_shape():
    resp = client.post(
        "/api/v1/reconcile",
        json={
            "settlements": [
                {"id": "S1", "date": "2026-11-01", "currency": "CNY", "amount_cents": -5}
            ],
            "bank_entries": [],
        },
    )
    assert resp.status_code == 422
    assert "detail" in resp.json()


def test_openapi_docs_available():
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/docs").status_code == 200
