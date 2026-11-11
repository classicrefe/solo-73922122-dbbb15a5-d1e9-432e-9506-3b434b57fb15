"""对账匹配核心求解器。

问题建模
========
每条银行入账可匹配 1~3 条同币种日结记录，要求：

* 金额合计严格相等（正整数分，无浮点误差）；
* 入账日在每条结算日之后 0~3 个自然日内（含端点）；
* 每条日结记录、每条入账在一个方案中至多使用一次。

目标：在所有合法方案中最大化「已匹配入账总金额」（加权集合装载 / weighted
set packing 问题）。日结记录数 n <= 12，用 2^n 位掩码做动态规划即可精确求解。

算法
====
1. 枚举每条入账的全部合法候选日结组（1~3 条、同币种、金额相等、日期合规）；
2. **按入账逐条推进**的前后缀 DP（保证每条入账至多被选一次）：
   - prefix[b][mask]：仅用前 b 条入账、占用 mask 中日结记录时的最大金额；
   - suffix[b][mask]：仅用第 b 条及以后入账时的最大金额；
3. 最优值 optimum = max(prefix[m])；
4. 入账 b 的候选组 cm 能进入某个最优方案，当且仅当存在与 cm 互不相交的
   前缀占用 p、后缀占用 s，使
   prefix[b][p] + amount(b) + suffix[b+1][s] == optimum。
   后缀侧用 SOS 子集最大值表加速；
5. 入账 b 只有 1 个最优兼容组 -> 所有最优方案中匹配关系唯一，可自动确认；
   多于 1 个 -> 存在歧义，转人工；
6. 带 suffix 上界剪枝的 DFS 枚举两套不同的完整最优方案用于展示。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from itertools import combinations
from typing import Sequence

from .schemas import MAX_DATE_DELTA_DAYS, MAX_SETTLEMENTS_PER_ENTRY

# DP 中不可达状态的标记
_NEG_INF = -10**18


@dataclass(frozen=True)
class _Record:
    id: str
    day: date
    currency: str
    amount: int


def _to_records(records: Sequence) -> list[_Record]:
    return [
        _Record(id=r.id, day=r.date, currency=r.currency, amount=r.amount_cents)
        for r in records
    ]


def _is_date_ok(bank_day: date, settlement_day: date) -> bool:
    """入账日须在结算日之后 0~3 个自然日内（含 0 和 3）。"""
    delta = (bank_day - settlement_day).days
    return 0 <= delta <= MAX_DATE_DELTA_DAYS


def _build_groups(
    settlements: list[_Record], bank_entries: list[_Record]
) -> list[list[int]]:
    """枚举每条入账的全部合法候选组，返回按入账索引组织的日结位掩码列表。"""
    n = len(settlements)
    groups: list[list[int]] = [[] for _ in bank_entries]
    for b, entry in enumerate(bank_entries):
        # 仅同币种、日期合规、单笔金额不超过入账金额的日结可参与
        eligible = [
            i
            for i, s in enumerate(settlements)
            if s.currency == entry.currency
            and _is_date_ok(entry.day, s.day)
            and s.amount <= entry.amount
        ]
        seen: set[int] = set()
        for size in range(1, MAX_SETTLEMENTS_PER_ENTRY + 1):
            for combo in combinations(eligible, size):
                if sum(settlements[i].amount for i in combo) != entry.amount:
                    continue
                mask = 0
                for i in combo:
                    mask |= 1 << i
                if mask not in seen:
                    seen.add(mask)
                    groups[b].append(mask)
    return groups


def _subset_max(table: list[int], size: int, n: int) -> list[int]:
    """SOS DP：g[mask] = max(table[s]) for all s ⊆ mask。"""
    g = table[:]
    for bit in range(n):
        step = 1 << bit
        for mask in range(size):
            if mask & step:
                candidate = g[mask ^ step]
                if candidate > g[mask]:
                    g[mask] = candidate
    return g


def _dp_step(prev: list[int], groups_b: list[int], amount: int) -> list[int]:
    """单条入账的 DP 转移：不匹配，或恰好选择它的一个候选组。"""
    cur = prev[:]  # 分支一：该入账不参与匹配
    for mask, value in enumerate(prev):
        if value == _NEG_INF:
            continue
        for cmask in groups_b:
            if mask & cmask == 0:
                new_mask = mask | cmask
                new_value = value + amount
                if new_value > cur[new_mask]:
                    cur[new_mask] = new_value
    return cur


def _build_prefix_suffix(
    groups: list[list[int]],
    bank_amounts: list[int],
    size: int,
) -> tuple[list[list[int]], list[list[int]]]:
    """按入账逐条推进的前后缀 DP，天然保证每条入账至多使用一次。"""
    m = len(groups)
    prefix = [[_NEG_INF] * size for _ in range(m + 1)]
    prefix[0][0] = 0
    for b in range(m):
        prefix[b + 1] = _dp_step(prefix[b], groups[b], bank_amounts[b])

    suffix = [[_NEG_INF] * size for _ in range(m + 1)]
    suffix[m][0] = 0
    for b in range(m - 1, -1, -1):
        suffix[b] = _dp_step(suffix[b + 1], groups[b], bank_amounts[b])
    return prefix, suffix


def _find_optimal_compatible_groups(
    groups: list[list[int]],
    bank_amounts: list[int],
    prefix: list[list[int]],
    suffix_max_tables: list[list[int]],
    all_mask: int,
    size: int,
    optimum: int,
) -> list[list[int]]:
    """筛选出能出现在某个最优方案中的候选组（按入账索引组织）。

    候选 cm（属于入账 b）最优兼容的充要条件：存在与 cm 互不相交的前缀占用 p
    和后缀占用 s，使 prefix[b][p] + amount(b) + suffix[b+1][s] == optimum。
    对每个前缀 p，后缀在 (all_mask ^ (p|cm)) 内的最大值用 SOS 子集表 O(1) 查询。
    ``suffix_max_tables[k]`` 为只使用第 k 条及以后入账时的 SOS 子集最大值表。
    """
    compatible: list[list[int]] = [[] for _ in groups]
    for b in range(len(groups)):
        if not groups[b]:
            continue
        suffix_max = suffix_max_tables[b + 1]
        amount = bank_amounts[b]
        for cmask in groups[b]:
            is_compatible = False
            for p_mask in range(size):
                p_value = prefix[b][p_mask]
                if p_value == _NEG_INF or p_mask & cmask:
                    continue
                free = all_mask ^ (p_mask | cmask)
                s_value = suffix_max[free]
                if s_value != _NEG_INF and p_value + amount + s_value == optimum:
                    is_compatible = True
                    break
            if is_compatible:
                compatible[b].append(cmask)
    return compatible


def _enumerate_solutions(
    groups: list[list[int]],
    compatible: list[list[int]],
    bank_amounts: list[int],
    suffix_max_tables: list[list[int]],
    all_mask: int,
    optimum: int,
    limit: int = 2,
) -> list[list[tuple[int, int]]]:
    """DFS 枚举不同的完整最优方案，找到 limit 套即停止。"""
    m = len(groups)
    solutions: list[list[tuple[int, int]]] = []
    chosen: list[tuple[int, int]] = []

    def search(b: int, used: int, total: int) -> None:
        if len(solutions) >= limit:
            return
        # 剩余入账在未占用日结上的理论上界都达不到 optimum -> 剪枝
        if total + suffix_max_tables[b][all_mask ^ used] < optimum:
            return
        if b == m:
            if total == optimum:
                solutions.append(list(chosen))
            return
        # 分支一：该入账不匹配
        search(b + 1, used, total)
        # 分支二：仅枚举最优兼容组（非兼容组不可能出现在最优方案中）
        for cmask in compatible[b]:
            if len(solutions) >= limit:
                return
            if used & cmask == 0:
                chosen.append((b, cmask))
                search(b + 1, used | cmask, total + bank_amounts[b])
                chosen.pop()

    search(0, 0, 0)
    return solutions


def solve(settlement_records: Sequence, bank_records: Sequence) -> dict:
    """执行对账求解，返回结构化结果（纯函数，便于单测）。"""
    settlements = _to_records(settlement_records)
    bank_entries = _to_records(bank_records)
    n = len(settlements)
    m = len(bank_entries)
    size = 1 << n
    all_mask = size - 1
    bank_amounts = [e.amount for e in bank_entries]

    groups = _build_groups(settlements, bank_entries)
    prefix, suffix = _build_prefix_suffix(groups, bank_amounts, size)
    optimum = max(prefix[m])

    # 每条后缀表的 SOS 子集最大值：兼容性判定与 DFS 剪枝共用，只计算一次
    suffix_max_tables = [
        _subset_max(suffix[b], size, n) for b in range(m + 1)
    ]
    compatible = _find_optimal_compatible_groups(
        groups,
        bank_amounts,
        prefix,
        suffix_max_tables,
        all_mask,
        size,
        optimum,
    )
    raw_solutions = _enumerate_solutions(
        groups,
        compatible,
        bank_amounts,
        suffix_max_tables,
        all_mask,
        optimum,
        limit=2,
    )

    def _settlement_ids(mask: int) -> list[str]:
        return [settlements[i].id for i in range(n) if mask & (1 << i)]

    # ---------- 组装方案输出 ----------
    solutions_out: list[dict] = []
    for chosen in raw_solutions:
        used_mask = 0
        matches: list[dict] = []
        matched_banks: set[int] = set()
        for b, cmask in sorted(chosen):
            entry = bank_entries[b]
            matches.append(
                {
                    "bank_entry_id": entry.id,
                    "settlement_ids": _settlement_ids(cmask),
                    "currency": entry.currency,
                    "amount_cents": entry.amount,
                }
            )
            used_mask |= cmask
            matched_banks.add(b)
        solutions_out.append(
            {
                "total_matched_cents": sum(bank_amounts[b] for b, _ in chosen),
                "matches": matches,
                "unmatched_settlement_ids": [
                    settlements[i].id
                    for i in range(n)
                    if not (used_mask & (1 << i))
                ],
                "unmatched_bank_entry_ids": [
                    bank_entries[b].id for b in range(m) if b not in matched_banks
                ],
            }
        )

    # ---------- 自动确认 / 人工核对 / 始终未匹配 ----------
    auto_confirm: list[dict] = []
    manual_review: list[dict] = []
    ever_matched_banks: set[int] = set()

    for b in range(m):
        comp_groups = compatible[b]
        if not comp_groups:
            continue  # 任何最优方案中都无法匹配
        ever_matched_banks.add(b)
        entry = bank_entries[b]
        if len(comp_groups) == 1:
            auto_confirm.append(
                {
                    "bank_entry_id": entry.id,
                    "settlement_ids": _settlement_ids(comp_groups[0]),
                    "currency": entry.currency,
                    "amount_cents": entry.amount,
                }
            )
        else:
            manual_review.append(
                {
                    "bank_entry_id": entry.id,
                    "currency": entry.currency,
                    "amount_cents": entry.amount,
                    "candidate_settlement_groups": [
                        _settlement_ids(cmask) for cmask in comp_groups
                    ],
                }
            )

    ever_used_settlement_mask = 0
    for comp_groups in compatible:
        for cmask in comp_groups:
            ever_used_settlement_mask |= cmask

    never_matched = {
        "settlements": [
            settlements[i].id
            for i in range(n)
            if not (ever_used_settlement_mask & (1 << i))
        ],
        "bank_entries": [
            bank_entries[b].id for b in range(m) if b not in ever_matched_banks
        ],
    }

    return {
        "optimal_total_matched_cents": optimum,
        "total_optimal_solutions": len(raw_solutions),
        "is_unique": len(raw_solutions) == 1,
        "solutions": solutions_out,
        "auto_confirm": auto_confirm,
        "manual_review": manual_review,
        "never_matched": never_matched,
    }
