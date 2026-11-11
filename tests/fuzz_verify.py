"""暴力穷举参考实现 + 随机输入交叉验证（不进 pytest 主套件，单独运行）。

运行方式：PYTHONPATH=<site-packages> python3 tests/fuzz_verify.py
"""

from __future__ import annotations

import random
import sys
from datetime import date, timedelta
from itertools import product

from app.schemas import ReconcileRequest
from app.solver import solve

CURRENCIES = ["CNY", "USD", "EUR"]


def brute_force(payload: dict) -> tuple[int, list[dict]]:
    """枚举每条入账的全部选择（不匹配 / 任一合法组），返回 (最优值, 全部最优方案)。"""
    req = ReconcileRequest.model_validate(payload)
    settlements = req.settlements
    banks = req.bank_entries
    n = len(settlements)

    options_per_bank: list[list[frozenset[int]]] = [[] for _ in banks]
    for b, entry in enumerate(banks):
        options: list[frozenset[int]] = [frozenset()]  # 不匹配
        for i, s in enumerate(settlements):
            if s.currency != entry.currency:
                continue
            delta = (entry.date - s.date).days
            if not (0 <= delta <= 3) or s.amount_cents > entry.amount_cents:
                continue
            for j in range(i + 1, n):
                s2 = settlements[j]
                if s2.currency != entry.currency:
                    continue
                d2 = (entry.date - s2.date).days
                if not (0 <= d2 <= 3):
                    continue
                if s.amount_cents + s2.amount_cents == entry.amount_cents:
                    options.append(frozenset({i, j}))
                for k in range(j + 1, n):
                    s3 = settlements[k]
                    if s3.currency != entry.currency:
                        continue
                    d3 = (entry.date - s3.date).days
                    if not (0 <= d3 <= 3):
                        continue
                    if (
                        s.amount_cents + s2.amount_cents + s3.amount_cents
                        == entry.amount_cents
                    ):
                        options.append(frozenset({i, j, k}))
            if s.amount_cents == entry.amount_cents:
                options.append(frozenset({i}))
        # 去重
        options_per_bank[b] = list(dict.fromkeys(options))

    best_total = -1
    best_solutions: list[dict] = []
    for combo in product(*options_per_bank):
        used: set[int] = set()
        total = 0
        ok = True
        matches = []
        for b, group in enumerate(combo):
            if not group:
                continue
            if used & group:
                ok = False
                break
            used |= group
            total += banks[b].amount_cents
            matches.append((b, tuple(sorted(group))))
        if not ok:
            continue
        if total > best_total:
            best_total = total
            best_solutions = [{"matches": matches}]
        elif total == best_total:
            best_solutions.append({"matches": matches})
    return best_total, best_solutions


def random_payload(rng: random.Random) -> dict:
    base = date(2026, 11, 1)
    n = rng.randint(0, 8)
    m = rng.randint(0, 8)
    settlements = []
    for i in range(n):
        settlements.append(
            {
                "id": f"S{i:02d}",
                "date": (base + timedelta(days=rng.randint(0, 6))).isoformat(),
                "currency": rng.choice(CURRENCIES),
                "amount_cents": rng.randint(1, 300),
            }
        )
    banks = []
    for i in range(m):
        banks.append(
            {
                "id": f"B{i:02d}",
                "date": (base + timedelta(days=rng.randint(0, 9))).isoformat(),
                "currency": rng.choice(CURRENCIES),
                "amount_cents": rng.randint(1, 600),
            }
        )
    return {"settlements": settlements, "bank_entries": banks}


def main() -> int:
    rng = random.Random(20261111)
    rounds = 400
    for round_idx in range(rounds):
        payload = random_payload(rng)
        req = ReconcileRequest.model_validate(payload)
        result = solve(req.settlements, req.bank_entries)

        best_total, best_solutions = brute_force(payload)
        assert result["optimal_total_matched_cents"] == best_total, (
            f"round {round_idx}: optimum mismatch "
            f"{result['optimal_total_matched_cents']} != {best_total}\n{payload}"
        )

        # 方案合法性：每条日结至多一次、金额相等、日期/币种合规
        for sol in result["solutions"]:
            used_ids: list[str] = []
            total = 0
            for match in sol["matches"]:
                total += match["amount_cents"]
                used_ids.extend(match["settlement_ids"])
                assert 1 <= len(match["settlement_ids"]) <= 3
            assert len(used_ids) == len(set(used_ids)), "settlement reused"
            assert total == best_total

        # 唯一/歧义标记与暴力解数量一致（暴力解 >1 时至少要返回 2 套不同方案）
        if len(best_solutions) == 1:
            assert result["is_unique"] is True
            assert len(result["solutions"]) == 1
        else:
            assert result["is_unique"] is False
            assert len(result["solutions"]) == 2
            sigs = [
                tuple(sorted(m["bank_entry_id"] + ":" + "-".join(m["settlement_ids"])
                             for m in s["matches"]))
                for s in result["solutions"]
            ]
            assert sigs[0] != sigs[1], "two returned solutions are identical"

        # 自动确认分类：与「全部最优方案中该入账的日结组是否恒定」交叉核对
        bank_groups: dict[str, set[tuple[str, ...]]] = {}
        for sol in best_solutions:
            seen_in_sol = set()
            for b, group in sol["matches"]:
                bid = req.bank_entries[b].id
                key = tuple(req.settlements[i].id for i in group)
                bank_groups.setdefault(bid, set()).add(key)
                seen_in_sol.add(bid)
        expected_auto = {bid for bid, gs in bank_groups.items() if len(gs) == 1}
        expected_manual = {bid for bid, gs in bank_groups.items() if len(gs) > 1}
        actual_auto = {item["bank_entry_id"] for item in result["auto_confirm"]}
        actual_manual = {item["bank_entry_id"] for item in result["manual_review"]}
        assert actual_auto == expected_auto, (
            f"round {round_idx}: auto mismatch {actual_auto} != {expected_auto}\n{payload}"
        )
        assert actual_manual == expected_manual

    print(f"OK: {rounds} random rounds cross-validated against brute force")
    return 0


if __name__ == "__main__":
    sys.exit(main())
