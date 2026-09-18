# 扭矩扳手复核 API（Torque Wrench Verification API）

装配线复核扭矩扳手的纯后端 HTTP API。接收目标扭矩与恰好五次读数，
以**十进制定点**（`decimal.Decimal`）完成全部计算与判定，杜绝浮点误差
导致临界样本在不同终端得到相反结论。复核结果可按**扭矩扳手编号**登记，
形成可追溯的设备健康档案，由连续结果自动给出**在用 / 观察 / 停用**状态。
误登记到错误扳手的读数可按**编号 + 登记序号作废**：原快照与作废原因
全部保留（审计证据不丢失），档案状态按仍有效的历史自动重算。

## 计算规则

- **平均值** = 五次读数之和 ÷ 5
- **带符号偏差率** = (平均值 − 目标值) ÷ 目标值 × 100%
- **极差率** = (最大值 − 最小值) ÷ 目标值 × 100%
- **内部比较不舍入**：判定一律使用未舍入的精确值；仅响应展示按
  四舍五入（ties 远离零）保留两位小数。
- **合格条件**：|偏差率| ≤ 2.00% **且** 极差率 ≤ 3.00%；等于边界仍合格。
- 输入约束：`target_nm` 与每个 `measured_nm` 均须在 **1.00–500.00 N·m**
  之间且**最多两位小数**，读数**恰好 5 个**；任一数值非法即整体拒绝，
  返回结构化错误（HTTP 422）。
- 请求体中的 JSON 数字按**原始文本精确解析为 `Decimal`**
  （`json.loads(..., parse_float/parse_int=Decimal)`），全程不经过
  二进制浮点。因此 `100.000000000000001`、`0.99999999999999999`、
  `500.00000000000001` 这类会被 float64 归并为 `100/1/500` 的临界值，
  仍按其真实文本判定为“超过两位小数”或“越界”，单次与批量接口均
  整体拒绝（批量不产生任何部分结果）。数值字符串输入同理。

> 临界示例：读数 `[102.00, 102.00, 102.00, 102.00, 102.02]`、目标 100.00
> 时，平均值为 102.004，偏差率 2.004%。响应中 `deviation_pct` 展示为
> `"2.00"`，但因内部比较使用未舍入值，`overall` 为 `"fail"`。
> 由于输入最多两位小数，偏差率商若不等于 2.00 至少相差 2×10⁻⁵，
> 而 40 位十进制精度误差约 10⁻³⁸ 量级，因此临界判定在任何终端稳定一致。

## 快速开始

### Docker Compose（推荐）

```bash
# 构建并启动 API（宿主端口默认 8000，可用 API_PORT 覆盖）
API_PORT=9000 docker compose up --build api

# 运行一次性验收服务：先执行 pytest，再对 API 做黑盒验收，随后退出
docker compose up --build --exit-code-from verify --abort-on-container-exit verify
```

`verify` 服务等待 `api` 健康后运行，验收内容：同一临界输入重复请求
响应逐字节一致、非法样本仅返回结构化错误、双项超限同时暴露两个原因、
边界相等判合格。全部通过时退出码为 0。

### 本地运行（Python 3.12）

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 运行测试

```bash
python -m pytest          # 计算、错误链路、批量、校准档案与作废共 163 项测试
API_BASE_URL=http://localhost:8000 python -m verify.acceptance   # 黑盒验收
```

## API

### `POST /api/v1/torque/verify`

请求体（JSON 数值或数值字符串均可）：

```json
{
  "target_nm": 100.00,
  "measured_nm": [100.10, 99.90, 100.00, 100.05, 99.95]
}
```

合格响应（HTTP 200）：

```json
{
  "target_nm": "100.00",
  "measured_nm": ["100.10", "99.90", "100.00", "100.05", "99.95"],
  "mean_nm": "100.00",
  "deviation_pct": "0.00",
  "deviation_limit_pct": "2.00",
  "deviation_ok": true,
  "range_pct": "0.20",
  "range_limit_pct": "3.00",
  "range_ok": true,
  "overall": "pass",
  "failure_reasons": []
}
```

| 字段 | 含义 |
| --- | --- |
| `target_nm` | 目标扭矩（两位小数的定点字符串回显） |
| `measured_nm` | 原始读数（规范化为两位小数，数值不变） |
| `mean_nm` | 平均值（展示值，四舍五入两位小数） |
| `deviation_pct` / `range_pct` | 带符号偏差率 / 极差率（展示值，%） |
| `deviation_limit_pct` / `range_limit_pct` | 各自限值：2.00% / 3.00% |
| `deviation_ok` / `range_ok` | 逐项结果（基于未舍入精确值判定） |
| `overall` | 总结果：`"pass"` / `"fail"` |
| `failure_reasons` | 不合格原因列表，双项超限时同时包含 `deviation_pct_out_of_limit` 与 `range_pct_out_of_limit` |

### `POST /api/v1/torque/verify/batch`

批量复核：一次提交 **1–20 个**测点（每个测点格式与单次接口请求体相同），
按输入顺序逐项复用同一十进制定点计算与同一响应映射，因此每项的字段和值
与逐个调用单次接口完全一致。合法批次中即使存在不合格测点也返回
HTTP 200，并计算全部测点（不因首个失败提前终止）。

请求体：

```json
{
  "items": [
    {"target_nm": 100.00, "measured_nm": [100.10, 99.90, 100.00, 100.05, 99.95]},
    {"target_nm": 100.00, "measured_nm": [102.00, 102.00, 102.00, 102.00, 102.02]}
  ]
}
```

响应（HTTP 200）：`items` 为逐项完整结果（结构同上表单次响应），
其后为整批汇总：

```json
{
  "items": [ /* 逐项完整结果，与单次接口字段和值一致 */ ],
  "total": 2,
  "passed_count": 1,
  "failed_count": 1,
  "failed_indices": [1],
  "overall": "fail"
}
```

| 字段 | 含义 |
| --- | --- |
| `items` | 逐项完整复核结果，顺序与输入一致 |
| `total` | 测点总数 |
| `passed_count` / `failed_count` | 合格数 / 不合格数 |
| `failed_indices` | 不合格测点的输入序号（0 起始，与校验错误 `loc` 的下标约定一致） |
| `overall` | 整批结果：全部合格为 `"pass"`，否则 `"fail"` |

空批次、超过 20 个测点或任一测点非法，均整体拒绝并返回与单次接口
相同的结构化错误（HTTP 422），不产生任何部分结果。

### `POST /api/v1/wrenches/{wrench_sn}/calibrations` — 登记一次复核

质检员完成单次复核后，把判定绑定到扭矩扳手编号。请求体与单次复核
接口**完全一致**（同一精确 Decimal 解析与判定，无第二套规则），
扳手编号在路径中：首尾空白会被去除、须为非空白且不超过 64 字符的字符串。

```bash
curl -X POST http://localhost:8000/api/v1/wrenches/TW-0001/calibrations \
  -H "Content-Type: application/json" \
  -d '{"target_nm": 100.00, "measured_nm": [100.10, 99.90, 100.00, 100.05, 99.95]}'
```

响应在**单次判定全部字段**之外，追加本次登记序号 `seq`、登记时刻
`registered_at`、扳手编号与迁移后的设备状态：

```json
{
  "target_nm": "100.00",
  "measured_nm": ["100.10", "99.90", "100.00", "100.05", "99.95"],
  "mean_nm": "100.00",
  "deviation_pct": "0.00",
  "deviation_limit_pct": "2.00",
  "deviation_ok": true,
  "range_pct": "0.20",
  "range_limit_pct": "3.00",
  "range_ok": true,
  "overall": "pass",
  "failure_reasons": [],
  "seq": 1,
  "registered_at": "2026-09-14T03:18:19+00:00",
  "is_valid": true,
  "void_reason": null,
  "voided_at": null,
  "wrench_sn": "TW-0001",
  "status": "in_service",
  "consecutive_fail_count": 0
}
```

**设备健康状态机**（由领域服务依据连续结果自动迁移）：

| 当前 | 本次结果 | 迁移后 | `consecutive_fail_count` |
| --- | --- | --- | --- |
| （首次登记） | 合格 | `in_service`（在用） | 0 |
| （首次登记） | 不合格 | `observation`（观察） | 1 |
| 在用 / 观察 | 首次出现不合格 | `observation`（观察） | 1 |
| 观察 | 再次不合格（连续两次） | `out_of_service`（停用） | 2 |
| 停用 | 继续不合格 | 保持 `out_of_service`（停用） | 继续累加 |
| **任意状态** | 任一后续合格 | `in_service`（在用） | **清零为 0** |

- **首次登记自动建立档案**；快照（完整单次判定字段）与档案状态在
  **同一个 SQLite 写事务**内原子提交，不会出现有快照无档案等中间态。
- 查询在**同一只读事务快照**内读取档案行与历史：边登记边查询时，
  响应中的 `status` / `consecutive_fail_count` 与 `history` 始终来自
  同一时刻，不会出现“仍显示在用/0、历史里却已有不合格”的撕裂视图
  （WAL 模式下读事务不阻塞登记）。
- 非法读数在契约层即被整体拒绝（HTTP 422），**不写入记录，也不改变
  既有状态**（编号首次出现时也不会因此建档）。

### `GET /api/v1/wrenches/{wrench_sn}/calibrations` — 查询健康档案

返回当前状态、连续不合格次数，以及**按登记顺序**排列的完整判定历史
（`seq` 从 1 起连续递增，每条含登记时刻与完整单次判定字段）。历史
包含已作废记录——作废不删除快照，每条记录补充 `is_valid` 及作废信息
（`void_reason` / `voided_at`），未作废记录的原字段保持不变：

```json
{
  "wrench_sn": "TW-0001",
  "status": "out_of_service",
  "consecutive_fail_count": 2,
  "total_records": 3,
  "history": [
    {"seq": 1, "registered_at": "…", "is_valid": true, "void_reason": null,
     "voided_at": null, "overall": "pass", "...": "…"},
    {"seq": 2, "registered_at": "…", "is_valid": true, "void_reason": null,
     "voided_at": null, "overall": "fail", "...": "…"},
    {"seq": 3, "registered_at": "…", "is_valid": true, "void_reason": null,
     "voided_at": null, "overall": "fail", "...": "…"}
  ]
}
```

**未知编号**返回与校验错误同外形的结构化错误（HTTP 404，
`error.code = "WRENCH_NOT_FOUND"`），不携带、不泄露任何其他档案的信息。

档案使用应用内 SQLite 持久化（默认 `data/calibration.db`，可用环境变量
`CALIBRATION_DB_PATH` 覆盖；docker compose 下挂载为命名卷
`calibration-data`）。

### `POST /api/v1/wrenches/{wrench_sn}/calibrations/{seq}/void` — 作废误登记

生产现场偶尔会把有效读数登记到错误扳手；直接删除会丢失审计证据，因此
作废只打标记、**保留原快照**。请求体为一至二百字的原因（`reason`，
首尾空白不计）：

```bash
curl -X POST http://localhost:8000/api/v1/wrenches/TW-0001/calibrations/3/void \
  -H "Content-Type: application/json" \
  -d '{"reason": "误登记到错误扳手，实际读数属于另一台设备"}'
```

作废成功后，领域服务**按登记顺序重放该档案中仍有效的判定**，重新计算
当前健康状态与连续不合格次数（作废末次不合格可恢复状态，作废中间记录
同样按剩余历史重算；全部作废则回到在用/0）。响应返回被作废序号及
重算后的档案摘要：

```json
{
  "wrench_sn": "TW-0001",
  "voided_seq": 3,
  "void_reason": "误登记到错误扳手，实际读数属于另一台设备",
  "voided_at": "2026-09-18T00:03:09+00:00",
  "status": "observation",
  "consecutive_fail_count": 1,
  "total_records": 3,
  "valid_records": 2
}
```

SQLite 在**同一事务**内标记记录、保存原因和作废时间并更新档案；查询
接口仍返回完整历史。错误情形（均不改动档案）：

- 序号不存在（或编号未建档）：HTTP 404，
  `error.code = "CALIBRATION_RECORD_NOT_FOUND"`，不泄露其他档案；
- 重复作废同一序号：HTTP 409，
  `error.code = "CALIBRATION_RECORD_ALREADY_VOIDED"`；
- 原因非法（空白、超过 200 字、非字符串）：HTTP 422
  `VALIDATION_ERROR`，在写入前整体拒绝。

### 示例

```bash
# 合格
curl -X POST http://localhost:8000/api/v1/torque/verify \
  -H "Content-Type: application/json" \
  -d '{"target_nm": 100.00, "measured_nm": [100.10, 99.90, 100.00, 100.05, 99.95]}'

# 临界不合格：偏差率 2.004%，展示 "2.00" 但 overall 为 "fail"
curl -X POST http://localhost:8000/api/v1/torque/verify \
  -H "Content-Type: application/json" \
  -d '{"target_nm": 100.00, "measured_nm": [102.00, 102.00, 102.00, 102.00, 102.02]}'

# 双项超限：failure_reasons 同时暴露两个原因
curl -X POST http://localhost:8000/api/v1/torque/verify \
  -H "Content-Type: application/json" \
  -d '{"target_nm": 100.00, "measured_nm": [95.00, 110.00, 105.00, 105.00, 105.00]}'

# 批量复核：一次提交多个测点，逐项判定并汇总
curl -X POST http://localhost:8000/api/v1/torque/verify/batch \
  -H "Content-Type: application/json" \
  -d '{"items": [
        {"target_nm": 100.00, "measured_nm": [100.10, 99.90, 100.00, 100.05, 99.95]},
        {"target_nm": 100.00, "measured_nm": [102.00, 102.00, 102.00, 102.00, 102.02]}
      ]}'

# 校准档案：把判定绑定到扳手编号（首次登记自动建档）
curl -X POST http://localhost:8000/api/v1/wrenches/TW-0001/calibrations \
  -H "Content-Type: application/json" \
  -d '{"target_nm": 100.00, "measured_nm": [102.00, 102.00, 102.00, 102.00, 102.02]}'

# 查询该扳手的当前状态、连续不合格次数与完整判定历史
curl http://localhost:8000/api/v1/wrenches/TW-0001/calibrations

# 作废误登记（保留快照与原因），按仍有效的历史重算档案状态
curl -X POST http://localhost:8000/api/v1/wrenches/TW-0001/calibrations/1/void \
  -H "Content-Type: application/json" \
  -d '{"reason": "误登记到错误扳手，实际读数属于另一台设备"}'
```

### 结构化错误（HTTP 422 / 404）

任一数值越界、小数位超过两位、读数个数不为 5、非数值或非法 JSON，
均整体拒绝，不返回任何结果字段：

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "请求未通过校验，已整体拒绝：……",
    "details": [
      {"loc": ["body", "measured_nm", 0], "type": "value_error",
       "msg": "Value error, value must have at most 2 decimal places"}
    ]
  }
}
```

### 其他端点

- `GET /health` — 健康检查，返回 `{"status": "ok"}`
- `GET /docs` — Swagger UI 交互文档

未知扳手编号的档案查询返回同一 `error` 外形（HTTP 404；登记入口会
自动建档，不返回此错误）：

```json
{
  "error": {
    "code": "WRENCH_NOT_FOUND",
    "message": "未找到该扭矩扳手编号的校准档案，请先完成一次登记。",
    "details": [
      {"loc": ["path", "wrench_sn"], "type": "not_found",
       "msg": "calibration profile not found"}
    ]
  }
}
```

## 项目结构

```
app/
  calculator.py     # 十进制定点计算核心（判定不舍入）
  parsing.py        # 请求体精确 JSON 解析（数字直转 Decimal，不经 float64）
  models.py         # 请求/响应契约与输入约束（Pydantic，仅做声明与校验）
  serialization.py  # 复核结果 → 响应模型的唯一映射（单次/批量/档案共用）
  db.py             # 应用内 SQLite 仓库（档案 + 复核快照，事务原子写入；作废审计列自动迁移）
  calibration.py    # 校准档案领域服务：状态迁移/重放纯函数与登记/查询/作废编排
  main.py           # FastAPI 路由，只做编排
tests/
  test_calculator.py    # 计算链路：边界、临界、舍入、确定性
  test_api.py           # 错误链路与 API 行为（含临界精度解析）
  test_batch.py         # 批量复核：编排汇总、逐项等价、整体拒绝（含临界精度）
  test_calibration.py   # 校准档案：建档、状态迁移、清零恢复、非法不写入、隔离、持久化
  test_void.py          # 误登记作废：重放重算、审计保留、409/404/422、重启一致
verify/
  acceptance.py     # 一次性黑盒验收（docker compose 的 verify 服务）
Dockerfile
docker-compose.yml  # api 服务（API_PORT 覆盖宿主端口、calibration-data 卷）+ verify 服务
requirements.txt
```
