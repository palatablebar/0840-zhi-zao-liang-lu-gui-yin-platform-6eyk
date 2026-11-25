# 接口

- `GET /`：工作台。
- `GET /api/catalog`：阶段、字段和样例。
- `POST /api/stages/{stage}`：按阶段返回处理结果。
- `POST /api/runs`：执行完整批次并保存草稿。
- `GET /api/runs/{id}`：读取批次。
- `POST /api/runs/{id}/review`：签发或暂缓，提交 `decision`、`comment`、`expected_revision`。
- `GET /api/runs/{id}/evidence`：下载已签发证据。
- `GET /api/examples/{profile}`：下载样例。
- `GET /healthz`：健康检查。

## 班前返工排程

- `GET /shift`：返工排程工作台。
- `GET /api/shifts`：班次列表与排程、签发状态。
- `POST /api/shifts`：新建或更新班次（工位工时、起止）。可带 `expected_revision`。
- `GET /api/shifts/{id}`：读班次、返工单与最近排程。
- `POST /api/shifts/{id}/jobs`：补录返工单（预计工时、交期、可救件数、实收数）。
- `GET /api/shifts/{id}/jobs`：列出返工单及派生状态。
- `POST /api/shifts/{id}/jobs/{job_id}/start`：登记开工（固定工位与开工时刻，之后不可挪）。
- `POST /api/shifts/{id}/schedule`：重算排程，可带 `expected_revision`；并发用旧版本会得到 `STALE_SCHEDULE`。
- `GET /api/shifts/{id}/plan`：读当前排程与返工单。
- `POST /api/shifts/{id}/review`：签发或暂缓，提交 `decision`、`comment`、`expected_revision`。
- `GET /api/shifts/{id}/evidence`：下载已签发的班次证据。
