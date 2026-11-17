# 实验室 FHIR R4 报告接入研究库（Lab FHIR Research Ingest API）

FastAPI + PostgreSQL 实现的 FHIR R4 事务型 Bundle 接入服务。检验科报告包以
transaction Bundle 提交，服务在**单数据库事务**内完成条件建患、观测/报告的
新增或版本化更新，并通过**接入单号幂等账本**保证“同内容重试复用首次回执、
异内容拒绝、响应丢失/进程重启不重复应用”。

## 1. 一键启动（Docker Compose，双 API 实例 + PostgreSQL）

```bash
docker compose up --build
```

- 两个**独立 API 实例**（`api1`、`api2`，各自独立进程/容器，共享同一 PostgreSQL），
  任一实例均可受理请求；幂等租约与栅栏令牌保证跨实例的重试、崩溃恢复安全。
  - api1 / 交互式文档：<http://localhost:8001/docs>
  - api2 / 交互式文档：<http://localhost:8002/docs>
- OpenAPI：`/openapi.json`；健康检查：`/health`
- PostgreSQL：`localhost:5432`，库 `researchdb`，用户/密码 `research/research`

首次启动时 API 容器会等待数据库就绪并自动建表（`app/db_init.py`）。

### 重试与恢复演示

```bash
./scripts/demo_retry_recovery.sh
```

脚本依次演示：同文重试复用首次回执 → 异文 409 → 活请求在另一实例上被即时
409 且只执行一次 → 超过 60 秒阈值的遗留 pending 被恢复且只应用一次（含
`psql` 对账本终态的核对）。`/docs` 页面也可手动发起 `POST /fhir` 复现。

## 2. 提交报告包

`POST /fhir`，请求头必须携带接入单号：

```
Idempotency-Key: LAB-ACC-1001
Content-Type: application/json
```

示例（注意条目故意乱序：报告 → 观测 → 患者，依赖 `urn:uuid` 前向引用）：

```json
{
  "resourceType": "Bundle",
  "type": "transaction",
  "entry": [
    {
      "fullUrl": "urn:uuid:3c3f9d7e-0003-4b2a-9e1a-000000000001",
      "resource": {
        "resourceType": "DiagnosticReport",
        "status": "final",
        "code": {"coding": [{"system": "http://loinc.org", "code": "24323-8"}]},
        "subject": {"reference": "urn:uuid:3c3f9d7e-0001-4b2a-9e1a-000000000001"},
        "result": [
          {"reference": "urn:uuid:3c3f9d7e-0002-4b2a-9e1a-000000000001"}
        ]
      },
      "request": {"method": "POST", "url": "DiagnosticReport"}
    },
    {
      "fullUrl": "urn:uuid:3c3f9d7e-0002-4b2a-9e1a-000000000001",
      "resource": {
        "resourceType": "Observation",
        "status": "final",
        "code": {"coding": [{"system": "http://loinc.org", "code": "2345-7"}]},
        "subject": {"reference": "urn:uuid:3c3f9d7e-0001-4b2a-9e1a-000000000001"},
        "valueQuantity": {"value": 5.5, "unit": "mmol/L"}
      },
      "request": {"method": "POST", "url": "Observation"}
    },
    {
      "fullUrl": "urn:uuid:3c3f9d7e-0001-4b2a-9e1a-000000000001",
      "resource": {
        "resourceType": "Patient",
        "identifier": [{"system": "http://hospital/mrn", "value": "MRN-001"}]
      },
      "request": {"method": "POST", "url": "Patient"}
    }
  ]
}
```

### 更新（带 If-Match 的乐观锁）

```json
{
  "resourceType": "Bundle",
  "type": "transaction",
  "entry": [
    {
      "resource": {
        "resourceType": "Observation",
        "id": "<已有观测id>",
        "status": "final",
        "code": {"coding": [{"code": "2345-7"}]},
        "subject": {"reference": "Patient/<患者id>"},
        "valueQuantity": {"value": 6.8, "unit": "mmol/L"}
      },
      "request": {
        "method": "PUT",
        "url": "Observation/<已有观测id>",
        "ifMatch": "W/\"1\""
      }
    }
  ]
}
```

- `ifMatch` 与当前版本不一致 → `412 Precondition Failed`，整包拒绝。
- 更新包内引用已存在资源请使用绝对引用（`Patient/<id>`、`Observation/<id>`）；
  `urn:uuid` 仅在同一 Bundle 内、且对应条目存在时有效。

## 3. 回执与读取接口

提交成功返回 `transaction-response` Bundle：

- `entry[].response`：FHIR 风格的 status / etag / location；
- `resourceMap[]`：按请求顺序给出每个条目的 `resourceType / id / version / action / fullUrl` 映射。

读取接口（均支持按患者维度）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/patients?identifier=system|value` | 按标识查患者 |
| GET | `/patients/{id}` | 患者摘要（含报告/观测计数） |
| GET | `/patients/{id}/observations` | 该患者全部观测 |
| GET | `/patients/{id}/diagnostic-reports` | 该患者全部报告（含观测 id 列表） |
| GET | `/observations/{id}` | 单个观测（响应头带 `ETag: W/"v"`） |
| GET | `/diagnostic-reports/{id}` | 单个报告 |
| GET | `/observations/{id}/_history` | 观测全部历史版本 |
| GET | `/diagnostic-reports/{id}/_history` | 报告全部历史版本 |
| GET | `/patients/{id}/_history` | 患者历史版本 |

## 4. 业务规则与失败语义

1. **条目上限 20**，仅允许 `Patient / Observation / DiagnosticReport`；
   超限或类型不符 → `400`。
2. **患者条件创建**：按 `identifier.system/value` 命中则复用，否则新建；
   包内标识重复 → `400`；并发同标识由数据库唯一约束 + 行锁保证只建一份。
3. **乱序前向引用**：`urn:uuid` 在解析阶段统一绑定，与条目顺序无关；
   无法解析、引用不存在的外部资源 → `422`，整包拒绝。
4. **患者一致性**：每份报告与其全部观测必须同属一个患者；把观测改挂其他
   患者时，若已有引用报告未在**同一 Bundle** 内一并更正 → `422`。
5. **原子性**：任何校验/版本/引用失败都会回滚整个事务，不留任何资源或
   修改；其他患者已提交的数据不受影响，后续合法 Bundle 仍可正常提交。
6. **幂等、重试与恢复（`app/services/idempotency.py`）**：
   - 每个在途请求持有账本行的**租约**（`owner_id` + 单调递增的 `fence_version`
     栅栏令牌），并由后台心跳线程按 `IDEMPOTENCY_HEARTBEAT_SECONDS`（默认 20s）
     刷新 `heartbeat_at`。
   - **活请求不重复执行**：同 `Idempotency-Key` 的并发重试只要看到新鲜心跳，
     立即返回 `409`（不等待慢请求结束），Bundle 绝不会被执行第二次。
   - **完成后**：同内容（规范化 JSON 哈希，与键序/空白无关）直接返回首次回执，
     不重复落库；异内容返回 `409`。
   - **真正崩溃才恢复**：仅当 pending 行的心跳/时间戳超过
     `IDEMPOTENCY_STALE_SECONDS`（保持原默认 **60s 不变**）才判定属主已死。
     恢复是带 `(fence_version, state)` 谓词的原子条件 UPDATE（compare-and-swap），
     多实例并发恢复也**至多生效一次**；重启后状态仍在 PostgreSQL，可继续恢复。
   - **旧请求不能破坏新回执**：完成回执与失败清理都以
     `(owner_id, fence_version)` 为条件，被取代的“僵尸”旧请求既不能覆盖、也不能
     删除后来请求的回执；其资源写入随事务回滚，资源与回执仍在**同一事务原子提交**。
   - 失败的提交会释放幂等槽，修正后可用同一单号重试；FHIR 校验规则保持不变。
   - 慢单不阻塞其他工作：`POST /fhir` 为同步端点，由 FastAPI 线程池调度，
     事件循环仍可处理读取与其他患者的接入。
7. 所有错误均返回 FHIR `OperationOutcome`（`application/fhir+json`）。

## 5. 本地开发与自测（无需 Docker）

```bash
pip install -r requirements.txt
export DATABASE_URL="postgresql+psycopg2://research:research@localhost:5432/researchdb"
python -m app.db_init
uvicorn app.main:app --reload
pytest -q
```

- 未设置 `DATABASE_URL` 时，测试自动回退到离线 SQLite（`/tmp/researchdb_test.sqlite3`），
  全量功能用例均可运行；依赖行级锁的并发用例会自动跳过并在 PostgreSQL 下执行。
  SQLite 驱动的事务边界由 `app/database.py` 显式管理（`BEGIN`/`SAVEPOINT`/`ROLLBACK`），
  以保证与 PostgreSQL 一致的保存点回滚与原子性语义。
- 用 `DATABASE_URL=postgresql+psycopg2://… pytest -q` 可在真实 PostgreSQL 上运行
  全部用例（含同标识建患、If-Match 竞争、跨实例恢复 CAS、慢单不阻塞等并发场景）。

## 6. 目录结构

```
app/
  main.py            FastAPI 入口、异常处理
  config.py          环境配置（含 60s 恢复阈值、20s 心跳）
  database.py        引擎/会话/建表（SQLite 事务修正）
  models.py          ORM（患者/标识/观测/报告/关联/版本/幂等账本，PG JSONB）
  errors.py          OperationOutcome 错误体系
  fhir_utils.py      ETag/版本工具
  routers/           /fhir 提交与资源读取路由
  services/
    idempotency.py   幂等租约、心跳、栅栏化恢复/完成/清理（本次核心）
    bundle.py        Bundle 校验、幂等编排、事务处理、患者一致性规则
    queries.py       读取与历史查询
scripts/entrypoint.sh          容器启动（等库+建表）
scripts/demo_retry_recovery.sh 双实例重试/恢复演示
tests/               pytest 用例（含重试恢复交错、并发、重启、原子性）
docker-compose.yml   db + api1 + api2 三服务，含健康检查
```
