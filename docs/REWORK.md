# 班前返工排程

在批次归因之后，工艺工程师每班开始前用**已签发的报工批次**为返工单排工位。
页面入口 `GET /shift`，接口见 `docs/HTTP.md`，代码在 `app/rework/`。

## 数据

### 班次 shift

```json
{
  "shift_id": "S-01",
  "starts_at": "2026-11-25T08:00:00",
  "ends_at": "2026-11-25T18:00:00",
  "stations": [
    {"station_id": "ST1", "name": "返工线1", "minutes": 600}
  ]
}
```

- `minutes` 是该工位本班可用工时（必须是正整数）。
- 同一 `shift_id` 重复提交会覆盖班次定义，并**清空旧排程与旧复核**（见下）。
- 若某工位上有已开工返工，不能把该工位从班次移除，否则返回 `STARTED_LOCKED`。
- 提交时可带 `expected_revision` 做并发保护，版本不符返回 `STALE_SCHEDULE`。

### 返工单 job

| 字段 | 说明 |
|---|---|
| `job_id` | 返工单号，主键 |
| `report_run_id` | 来源报工批次，必须已签发且无阻断项 |
| `work_order` | 工单号，可选；填了就必须出现在报工结果里 |
| `station_id` | 工位，必须在班次内；留空则待定 |
| `expected_minutes` | 预计工时（分钟，正整数） |
| `due_at` | 交期（ISO 本地时间） |
| `salvage_qty` | 可救件数（非负整数） |
| `report_qty` / `received_qty` | 报工数、实收数，用于对账 |
| `started` / `started_at` | 是否已开工及开工时刻 |

## 规则

1. **报工前置**：返工单必须引用已签发、无 `REJECT` 的报工批次；报工结果里要能查到对应工单。
2. **补录完整才可排**：缺 `expected_minutes`、`due_at` 或 `salvage_qty` 的返工单进入 `INCOMPLETE`，不占工时，等补录。
3. **已开工不可挪**：`started` 的返工固定在该工位、按真实 `started_at` 落位，先占用工时，不会被前移或后移。
   开工时刻早于班次只按班次起点计；与其它已开工单重叠只报 `STARTED_OVERLAP`，不重排。
4. **实收差异撤占用**：未开工的返工若 `received_qty != report_qty`，返回 `REVOKED`，不占工时，等对账。
   已开工的不撤销——开工事实不可回滚，仅报 `STARTED_MISMATCH` 供人工核对。
5. **急单整批优先**：交期落在本班内（或 `urgent=true`）为急单。只有整批能在本工位剩余工时内、
   且在交期前完成时才优先占用；赶不上的急单让出优先位，退回余量池按救料能力处理。
6. **余量多救料**：剩余工时用 0/1 背包整单排入，最大化可救件数；同值按交期先后落位。
   装不下的单进入 `deferred`，原因 `CAPACITY` / `NOT_SELECTED` / `DEADLINE` / `INCOMPLETE` / `RECEIPT_MISMATCH` / `NO_STATION`。
7. **不超占**：新排入的返工不会让工位 `used_minutes` 超过 `capacity_minutes`。
   已开工段本身已超出可用工时时如实落排（不截断、不挪动），标记 `CAPACITY_OVERFLOW` 并阻断签发。

## 并发

排程在单个 `BEGIN IMMEDIATE` 事务内读取返工单、计算并落库，`revision` 自增。
两个请求同时提交同一基础版本时只有一个成功，另一个收到 `STALE_SCHEDULE`，
因此不会出现两份排程各自占用同一工位导致超占。

## 复核与证据

- `POST /api/shifts/{id}/review` 需带 `expected_revision`；成功后排程读数 `revision` 再次自增。
  若排程存在超占（`totals.over_capacity`），签发返回 `GATE_BLOCKED`。
- 重排或改班次会清空复核，旧证据立即失效。
- `GET /api/shifts/{id}/evidence` 只在复核为 `APPROVED` 后返回，带 `artifact_sha256` 供校验。
