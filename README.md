# 支付渠道日结对账 API

核对外渠道**日结单**（扣费后应结金额）与**银行贷记流水**，防止合并打款被错误核销。
服务基于 FastAPI，提供精确的全局最优匹配求解、歧义检测与自动确认分类。

## 业务规则

| 规则 | 说明 |
| --- | --- |
| 匹配基数 | 一笔银行入账匹配 **1～3 条**同币种日结记录 |
| 金额 | 日结金额合计与入账金额**严格相等**，单位为正整数「分」，无浮点误差 |
| 日期 | 入账日在每条结算日之后 **0～3 个自然日**内（含 0 和 3） |
| 币种 | 必须同币种（币种码大小写不敏感，统一转大写处理） |
| 唯一性 | 每条日结记录 / 每条入账在一套方案中至多使用一次 |
| 规模 | 日结记录与银行流水各不超过 **12** 条 |

**优化目标**：最大化全局已匹配入账总金额（加权集合装载问题，n≤12 用位掩码 DP 精确求解）。

## 结果分类口径

- `auto_confirm`：在**所有**最优方案中都对应同一日结组的入账 → 可自动确认核销；
- `manual_review`：在不同最优方案中对应不同日结组的入账 → 有歧义，转人工核对，
  响应中列出该入账的全部候选日结组；
- `never_matched`：在任何最优方案中始终未被匹配的日结记录 / 入账，单列处理；
- `solutions`：唯一时返回 1 套最优方案；存在歧义时返回 **2 套不同的完整最优方案**。

## 目录结构

```
.
├── app/
│   ├── main.py        # FastAPI 入口、路由与交互式文档
│   ├── schemas.py     # Pydantic 请求/响应模型与输入校验
│   └── solver.py      # 核心求解器（前后缀位掩码 DP + SOS + DFS）
├── tests/
│   ├── test_solver.py # 27 个单元/集成测试
│   └── fuzz_verify.py # 暴力穷举参考实现的随机交叉验证（400 组）
├── examples/          # 样例请求与对应响应
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 方式一：Docker Compose 启动（推荐）

```bash
docker compose up --build
```

启动后：

- 交互式 API 文档（Swagger UI）：<http://localhost:8000/docs>
- ReDoc 文档：<http://localhost:8000/redoc>
- OpenAPI Schema：<http://localhost:8000/openapi.json>
- 健康检查：<http://localhost:8000/health>

容器以非 root 用户运行，内置 `HEALTHCHECK`。停止服务：`docker compose down`。

## 方式二：本地 Python 启动

需要 Python 3.11+：

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 样例请求

### 1. 唯一最优方案（自动确认 + 始终未匹配）

```bash
curl -X POST http://localhost:8000/api/v1/reconcile \
  -H 'Content-Type: application/json' \
  -d @examples/request_unique.json
```

要点：`BANK-CR-1001` 合并核销两条日结（10000+5800=15800）；`BANK-CR-1004`
因金额无法凑出而进入 `never_matched.bank_entries`。

### 2. 有歧义（返回两套不同最优方案，全部转人工）

```bash
curl -X POST http://localhost:8000/api/v1/reconcile \
  -H 'Content-Type: application/json' \
  -d @examples/request_ambiguous.json
```

两条 30000 分入账都可以由不同的「10000+20000」组合构成，`is_unique=false`，
响应包含两套不同的完整最优方案，两条入账均出现在 `manual_review` 中并列出候选组。

### 3. 边界场景（超日期窗口 / 币种不符 / 金额无法凑齐）

```bash
curl -X POST http://localhost:8000/api/v1/reconcile \
  -H 'Content-Type: application/json' \
  -d @examples/request_edge_cases.json
```

- `BANK-E2` 入账日距结算日 9 天，超出 3 天窗口；
- `BANK-E3` 为 USD，无同币种日结；
- `BANK-E4` 金额 99999 无法由任意 1～3 条日结凑出。

### 请求体字段

```json
{
  "settlements": [
    {"id": "STL-001", "date": "2026-11-01", "currency": "CNY", "amount_cents": 10000}
  ],
  "bank_entries": [
    {"id": "BANK-001", "date": "2026-11-02", "currency": "CNY", "amount_cents": 10000}
  ]
}
```

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `id` | string | 组内唯一，非空，最长 64 |
| `date` | string(`YYYY-MM-DD`) | ISO 日期 |
| `currency` | string(3) | ISO 4217 三位币种码，大小写不敏感 |
| `amount_cents` | integer | **正整数**分，严格 > 0 |

两组记录各允许 0～12 条；编号重复、金额非正、多余字段、超条数等均返回 `422`。

## 算法说明

1. 枚举每条入账的全部合法候选日结组（1～3 条、同币种、金额相等、日期合规）；
2. 按入账逐条推进的前/后缀位掩码 DP（2^n，n≤12），从机制上保证每条入账至多使用一次，
   求得全局最优匹配金额 `optimum`；
3. 利用 SOS 子集最大值表，判定每个候选组能否嵌入某个最优方案：
   - 某入账仅有 1 个最优兼容组 → 所有最优方案中映射恒定 → 自动确认；
   - 多于 1 个 → 存在歧义 → 人工核对；
4. 带上界剪枝的 DFS 枚举两套不同的完整最优方案；
5. 汇总任何最优方案中都未出现的记录为 `never_matched`。

满规模（12×12）极端高歧义输入的求解时间在 1 秒量级，常规输入为毫秒级。

## 测试

```bash
pip install -r requirements.txt
pytest                       # 27 个单元/集成测试
python tests/fuzz_verify.py  # 400 组随机输入与暴力穷举实现交叉验证
```

交叉验证会逐一比对最优值、方案合法性（记录不重复使用）、唯一/歧义标记以及
自动确认/人工核对分类，确保 DP 实现与独立暴力枚举完全一致。
