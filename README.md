# 支付渠道日结对账 + 银行回单采集 API

基于 FastAPI 的服务，包含两个独立业务入口：

1. **日结对账（保留原核对入口）**：核对外渠道日结单与银行贷记流水，求解全局最优匹配；
2. **银行回单采集（新增）**：按银行回单索引（流水 ID）从配置固定的银行 HTTP 源
   逐块下载完整原件，校验 SHA-256 后原子归档；支持按流水 ID 重新查询并下载原件。

回单原件保存在本地文件系统，SQLite **只保存索引**；服务重启后仍可查询和下载。

---

## 一、Docker Compose 启动（推荐）

```bash
docker compose up --build
```

启动后：

| 服务 | 地址 | 说明 |
| --- | --- | --- |
| API 进程 1 | <http://localhost:8000/docs> | Swagger UI，可演示采集与下载 |
| API 进程 2 | <http://localhost:8001/docs> | 同一镜像、同一数据卷的第二个进程 |
| 本地银行源 | <http://localhost:9000/docs> | 可模拟断流、坏摘要、超限回单 |
| 健康检查 | <http://localhost:8000/health> | `{"status":"ok"}` |
| 对账入口 | `POST /api/v1/reconcile` | 原核对入口保持不变 |

两个 API 服务（`reconcile-api` / `reconcile-api-2`）运行同一镜像的两个进程，并挂载同一个命名卷
`reconcile-receipts-data`。并发批次即使落在不同进程，也由共享 SQLite 索引和
写事务满足“同 ID 同摘要复用 / 同 ID 异摘要整批拒绝 / 整批原子发布”。

停止服务：

```bash
docker compose down          # 保留数据卷，重启后回单仍在
docker compose down -v       # 同时删除数据卷
```

### 在 /docs 中演示

1. 打开 <http://localhost:8000/docs>，展开 `GET /receipts`（本地银行源）可看到内置
   回单及其预期 SHA-256；也可以直接执行：

   ```bash
   curl -s http://localhost:9000/receipts | python3 -m json.tool
   ```

2. 在 API 文档中执行 `POST /api/v1/receipts/collect`，使用
   `examples/request_receipts_collect.json`（或直接粘贴下面的请求体），得到
   `published=2`。
3. 展开 `GET /api/v1/receipts/{transaction_id}`，填入 `RCPT-DEMO-001` 并 Execute，
   即可下载刚归档的完整原件（响应头 `X-Receipt-Sha256` / `X-Receipt-Size`）。

```bash
curl -X POST http://localhost:8000/api/v1/receipts/collect \
  -H 'Content-Type: application/json' \
  -d @examples/request_receipts_collect.json

curl -OJ http://localhost:8000/api/v1/receipts/RCPT-DEMO-001
```

---

## 二、回单采集 API

### 规则

| 规则 | 说明 |
| --- | --- |
| 批次规模 | 每批 **1～12** 项 |
| 流水 ID | 仅允许英文字母、数字、短横线，批内唯一；天然阻断路径注入 |
| 预期摘要 | SHA-256，64 位十六进制（大小写不敏感） |
| 银行源 | 由环境变量 `BANK_RECEIPT_BASE_URL` 固定配置，调用 `GET /receipts/{id}` |
| 单份上限 | **16 MiB**（16,777,216 字节，含等于；超过即拒绝） |
| 下载方式 | 逐块流式下载，内存中只保留一个块；同时增量计算 SHA-256 |
| 发布语义 | 本批**全部**成功才一次性发布；断流、超限、摘要错误、进程中断都不暴露半批 |
| 幂等 | 同 ID 同摘要直接复用已归档原件，不再访问银行源 |
| 冲突 | 同 ID 已归档为不同摘要时，以 `409` 拒绝**整批** |
| 持久化 | 原件为本地文件，SQLite 仅存索引；重启后仍可查询下载 |

### 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/receipts/collect` | 采集一批回单（主路径，同时接受 `/api/v1/receipts`、`/api/v1/receipts/batch`） |
| `GET` | `/api/v1/receipts/{transaction_id}` | 按流水 ID 下载完整原件（同时接受 `.../download`） |
| `GET` | `/api/v1/receipts/{transaction_id}/metadata` | 查询索引元数据 |
| `GET` | `/api/v1/receipts` | 列出全部已归档回单索引 |

请求体（`items` 也可写作 `receipts` / `requests` / `batch`；`transaction_id` 也可写作
`id`、`receipt_id`，`sha256` 也可写作 `expected_sha256`、`digest`）：

```json
{
  "items": [
    {
      "transaction_id": "RCPT-DEMO-001",
      "sha256": "6b7aa10c704750351c1d4bc9dbee466396644cc7ece1c82244a226eef4366a7a"
    }
  ]
}
```

成功响应：

```json
{
  "status": "published",
  "batch_id": "13b32af318d14d74ba0fcd22ccdd835c",
  "total": 2,
  "published": 2,
  "reused": 0,
  "receipts": [
    {
      "transaction_id": "RCPT-DEMO-001",
      "sha256": "6b7aa10c704750351c1d4bc9dbee466396644cc7ece1c82244a226eef4366a7a",
      "size_bytes": 100,
      "reused": false,
      "download_url": "/api/v1/receipts/RCPT-DEMO-001"
    }
  ]
}
```

错误响应统一包含 `code`、`message`、`batch_rejected: true` 及失败项
`transaction_id`：

| 状态码 | code | 场景 |
| --- | --- | --- |
| `422` | `digest_mismatch` | 下载内容摘要与预期不符（整批不发布） |
| `409` | `receipt_conflict` | 同 ID 已归档为不同摘要（整批拒绝） |
| `413` | `receipt_too_large` | 单份超过 16 MiB（整批不发布） |
| `502` | `stream_interrupted` | 响应流中断 / 提前结束（整批不发布） |
| `502` | `upstream_failure` | 银行源不可达、超时或非 200（整批不发布） |
| `422` | 校验错误 | 条数、ID 字符、重复 ID、摘要格式等不合法 |
| `503` | `store_busy` | 索引库持续繁忙（整批不发布） |

### 复用、冲突与整批原子性示例

```bash
# 1) 同 ID 同摘要：第二次采集直接复用，published=0, reused=1
curl -s -X POST http://localhost:8000/api/v1/receipts/collect \
  -H 'Content-Type: application/json' \
  -d '{"items":[{"transaction_id":"RCPT-DEMO-001","sha256":"6b7aa10c704750351c1d4bc9dbee466396644cc7ece1c82244a226eef4366a7a"}]}'

# 2) 同 ID 异摘要：409，整批拒绝；同批中的其它新回单也不会发布
curl -s -X POST http://localhost:8000/api/v1/receipts/collect \
  -H 'Content-Type: application/json' \
  -d '{"items":[{"transaction_id":"RCPT-DEMO-001","sha256":"1111111111111111111111111111111111111111111111111111111111111111"}]}'
```

---

## 三、断流 / 坏摘要 / 超限演示

本地银行源内置了故障模式，可随时切换：

| 回单 ID | 默认模式 | 现象 |
| --- | --- | --- |
| `RCPT-DEMO-TRUNC` | `truncate` | 声明完整 `Content-Length`，只发一半后断开 → `502 stream_interrupted` |
| `RCPT-DEMO-BAD` | `bad_digest` | 返回被篡改字节 → `422 digest_mismatch` |
| `RCPT-DEMO-BIG` | `oversize` | 持续发送超过 16 MiB → `413 receipt_too_large` |

演示“批内一项失败、整批不发布”：

```bash
# 第一项本身合法，第二项是坏摘要；整批被拒绝
curl -X POST http://localhost:8000/api/v1/receipts/collect \
  -H 'Content-Type: application/json' \
  -d @examples/request_receipts_bad_batch.json

# 两项都查不到（包括本应成功的第一项）
curl -i http://localhost:8000/api/v1/receipts/RCPT-DEMO-002
curl -i http://localhost:8000/api/v1/receipts/RCPT-DEMO-BAD
```

管理本地银行源（仅演示环境）：

```bash
# 新建 / 覆盖回单，返回其 SHA-256
curl -X PUT http://localhost:9000/admin/receipts/RCPT-TEST-001 \
  -H 'Content-Type: application/json' \
  -d '{"content":"custom receipt bytes","mode":"normal"}'

# 切换故障模式：normal | truncate | bad_digest | oversize | slow
curl -X POST http://localhost:9000/admin/receipts/RCPT-TEST-001/mode \
  -H 'Content-Type: application/json' \
  -d '{"mode":"truncate"}'

# 下载次数统计（验证同 ID 同摘要没有重复访问银行源）
curl -s http://localhost:9000/admin/stats

# 恢复内置演示数据
curl -X POST http://localhost:9000/admin/reset
```

---

## 四、并发与持久化设计

### 两个 API 进程

Compose 启动两个独立进程 `reconcile-api`（8000）和 `reconcile-api-2`（8001），挂载同一个数据卷。
两个进程可以同时处理**包含重叠 ID** 的批次：

* 同 ID 同摘要：先提交者发布，后提交者在写事务内看到同摘要并复用；两项都成功；
* 同 ID 异摘要：先提交者成功，后提交者在写事务内检测到冲突并**回滚整批**；
* 失败批次只清理自己的 `staging/<batch_id>/` 目录，**从不删除** `objects/` 中
  已被另一批发布或正在被另一批使用的文件。

### 无半批暴露的发布流程

```
下载校验阶段（不持数据库写锁）
  staging/<batch_id>/00.part ...   ← 逐块写入并增量校验 SHA-256
        │
        │ 全部 1~12 项均成功
        ▼
放入 objects/<sha256>              ← 内容寻址，已存在则直接复用
        │
        ▼
BEGIN IMMEDIATE; 再次检查重叠 ID; 插入整批索引; COMMIT
        │
        ▼
查询端只读 SQLite 索引 → 整批同时可见
```

* **断流 / 超限 / 摘要错误**：在下载校验阶段失败，事务尚未开始，整批不可见；
* **进程中断（发布前）**：最多留下未被索引的暂存/孤儿文件，查询端不可见；
* **进程中断（提交时）**：SQLite 事务原子回滚或原子提交，不会出现半批索引；
* **网络等待**：下载阶段完全不持有数据库写锁，`BEGIN IMMEDIATE` 只包住最后的
  索引检查与插入；SQLite 使用 WAL + `busy_timeout`，两个进程的读/写可并发。

### 存储布局

```
/data
├── receipts.db          # 仅索引：transaction_id, sha256, size_bytes, created_at
├── receipts.db-wal      # SQLite WAL
├── objects/
│   └── <sha256>         # 原件字节（本地文件，可被同摘要复用）
└── staging/
    └── <batch_id>/      # 批次暂存；失败/成功结束后清理
```

重启（`docker compose restart` 或重建容器）后，命名卷中的索引与原件保留。

---

## 五、本地 Python 启动（不使用 Docker）

需要 Python 3.11+。先分别启动银行源和 API：

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 终端 1：本地银行源
uvicorn bank_source.main:app --host 127.0.0.1 --port 9000

# 终端 2：API（可用两次启动两个端口模拟两个进程）
BANK_RECEIPT_BASE_URL=http://127.0.0.1:9000 \
RECEIPT_DATA_DIR=/tmp/receipt-data \
  uvicorn app.main:app --host 127.0.0.1 --port 8000
```

打开 <http://localhost:8000/docs> 或 <http://localhost:9000/docs>。

---

## 六、原有日结对账功能（保留）

### 业务规则

| 规则 | 说明 |
| --- | --- |
| 匹配基数 | 一笔银行入账匹配 **1～3 条**同币种日结记录 |
| 金额 | 日结金额合计与入账金额**严格相等**，单位为正整数「分」，无浮点误差 |
| 日期 | 入账日在每条结算日之后 **0～3 个自然日**内（含 0 和 3） |
| 币种 | 必须同币种（币种码大小写不敏感，统一转大写处理） |
| 唯一性 | 每条日结记录 / 每条入账在一套方案中至多使用一次 |
| 规模 | 日结记录与银行流水各不超过 **12** 条 |

**优化目标**：最大化全局已匹配入账总金额（加权集合装载问题，n≤12 用位掩码 DP 精确求解）。

### 结果分类口径

- `auto_confirm`：在**所有**最优方案中都对应同一日结组的入账 → 可自动确认核销；
- `manual_review`：在不同最优方案中对应不同日结组的入账 → 有歧义，转人工核对，
  响应中列出该入账的全部候选日结组；
- `never_matched`：在任何最优方案中始终未被匹配的日结记录 / 入账，单列处理；
- `solutions`：唯一时返回 1 套最优方案；存在歧义时返回 **2 套不同的完整最优方案**。

### 样例请求

```bash
# 唯一最优方案
curl -X POST http://localhost:8000/api/v1/reconcile \
  -H 'Content-Type: application/json' \
  -d @examples/request_unique.json

# 有歧义（返回两套不同最优方案）
curl -X POST http://localhost:8000/api/v1/reconcile \
  -H 'Content-Type: application/json' \
  -d @examples/request_ambiguous.json

# 边界场景
curl -X POST http://localhost:8000/api/v1/reconcile \
  -H 'Content-Type: application/json' \
  -d @examples/request_edge_cases.json
```

请求体字段：

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

`id` 组内唯一、最长 64；`date` 为 ISO 日期；`currency` 为三位币种码；
`amount_cents` 为严格正整数；两组记录各 0～12 条；编号重复、金额非正、多余字段、
超条数等均返回 `422`。

### 算法说明

1. 枚举每条入账的全部合法候选日结组（1～3 条、同币种、金额相等、日期合规）；
2. 按入账逐条推进的前/后缀位掩码 DP（2^n，n≤12），保证每条入账至多使用一次，
   求得全局最优匹配金额；
3. 利用 SOS 子集最大值表判定每个候选组能否嵌入某个最优方案，区分自动确认与歧义；
4. 带上界剪枝的 DFS 枚举两套不同的完整最优方案；
5. 汇总任何最优方案中都未出现的记录为 `never_matched`。

---

## 七、目录结构

```
.
├── app/
│   ├── main.py              # FastAPI 入口、lifespan、原对账路由
│   ├── schemas.py           # 原对账请求/响应模型
│   ├── solver.py            # 原对账求解器
│   ├── config.py            # 银行源地址 / 数据目录 / 大小与超时配置
│   ├── receipt_schemas.py   # 回单采集请求/响应模型与校验
│   ├── receipt_fetch.py     # 逐块下载、16MiB 上限、摘要校验
│   ├── receipt_store.py     # SQLite 索引、内容寻址存储、原子发布
│   └── receipts.py          # 采集/查询/下载路由
├── bank_source/
│   └── main.py              # 本地银行源（正常 / 断流 / 坏摘要 / 超限 / 慢速）
├── tests/
│   ├── test_solver.py       # 原对账 27 个测试
│   ├── test_receipts.py     # 回单采集、原子性、并发、持久化测试
│   └── fuzz_verify.py       # 原对账随机交叉验证
├── examples/                # 对账与回单采集样例
├── Dockerfile               # 同一镜像可运行 API 或本地银行源
├── docker-compose.yml       # reconcile-api + reconcile-api-2 + bank-source + 共享数据卷
└── requirements.txt
```

---

## 八、测试

```bash
pip install -r requirements.txt
pytest                       # 原对账 + 回单采集全部测试
python tests/fuzz_verify.py  # 对账求解器与暴力穷举交叉验证（400 组）
```

回单测试覆盖：正常采集/下载/元数据/重启保留、同 ID 同摘要复用不重复访问源、
异摘要整批拒绝、断流、超限、摘要错误整批回滚、失败清理不删除他批已发布文件、
多线程重叠批次并发、慢速下载期间不阻塞其它发布，以及 SQLite 仅存索引。
