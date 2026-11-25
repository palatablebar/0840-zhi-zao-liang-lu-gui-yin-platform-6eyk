import threading

from app.main import create_app


SHIFT_BODY = {
    "shift_id": "S-01",
    "starts_at": "2026-11-25T08:00:00",
    "ends_at": "2026-11-25T18:00:00",
    "stations": [
        {"station_id": "ST1", "name": "返工线1", "minutes": 600},
        {"station_id": "ST2", "name": "返工线2", "minutes": 300},
    ],
}


def approved_report(client, profile="clean"):
    run = client.post("/api/runs", json={"profile": profile}).get_json()
    client.post(f"/api/runs/{run['run_id']}/review",
                json={"decision": "APPROVED", "comment": "报工签发", "expected_revision": 0})
    return run["run_id"]


def make_shift(client, body=None, expected=None):
    payload = dict(body or SHIFT_BODY)
    if expected is not None:
        payload["expected_revision"] = expected
    return client.post("/api/shifts", json=payload)


def add_job(client, report_id, job_id, **changes):
    payload = {
        "job_id": job_id,
        "report_run_id": report_id,
        "work_order": changes.pop("work_order", "WO1"),
        "station_id": "ST1",
        "expected_minutes": 60,
        "due_at": "2026-11-25T17:00:00",
        "salvage_qty": 10,
        "report_qty": 10,
        "received_qty": 10,
        "started": False,
    }
    payload.update(changes)
    return client.post("/api/shifts/S-01/jobs", json=payload)


def plan_of(client):
    return client.get("/api/shifts/S-01/plan").get_json()


def test_shift_requires_approved_report(client):
    client.post("/api/runs", json={"profile": "dirty"})
    make_shift(client)
    response = add_job(client, "missing-run", "J1")
    assert response.status_code == 409
    assert response.get_json()["code"] == "REPORT_NOT_APPROVED"


def test_draft_report_cannot_schedule(client):
    run = client.post("/api/runs", json={"profile": "clean"}).get_json()
    make_shift(client)
    response = add_job(client, run["run_id"], "J1")
    assert response.status_code == 409
    assert response.get_json()["code"] == "REPORT_NOT_APPROVED"


def test_job_must_be_complete(client):
    report = approved_report(client)
    make_shift(client)
    response = add_job(client, report, "J1", due_at=None)
    assert response.status_code == 200
    assert response.get_json()["status"] == "INCOMPLETE"
    plan = client.post("/api/shifts/S-01/schedule", json={}).get_json()
    assert plan["schedule"]["totals"]["jobs_scheduled"] == 0
    assert plan["schedule"]["deferred"][0]["reason"] == "INCOMPLETE"


def test_salvage_cannot_exceed_receipt(client):
    report = approved_report(client)
    make_shift(client)
    response = add_job(client, report, "J1", salvage_qty=11, report_qty=12, received_qty=10)
    assert response.status_code == 400
    assert response.get_json()["code"] == "SALVAGE_OVER_RECEIPT"


def test_started_job_is_locked_in_place(client):
    report = approved_report(client)
    make_shift(client)
    add_job(client, report, "J1", started=True, started_at="2026-11-25T08:00:00",
            expected_minutes=120, salvage_qty=8)
    add_job(client, report, "J2", expected_minutes=60, salvage_qty=10)
    plan = client.post("/api/shifts/S-01/schedule", json={}).get_json()["schedule"]
    station = next(item for item in plan["stations"] if item["station_id"] == "ST1")
    assert station["items"][0]["job_id"] == "J1"
    assert station["items"][0]["role"] == "STARTED"
    assert station["items"][0]["start_at"] == "2026-11-25T08:00:00"


def test_started_job_cannot_be_moved_after_start(client):
    report = approved_report(client)
    make_shift(client)
    add_job(client, report, "J1", started=True, started_at="2026-11-25T08:00:00", expected_minutes=120)
    moved = client.post("/api/shifts/S-01/jobs/J1/start",
                        json={"station_id": "ST2", "started_at": "2026-11-25T09:00:00"})
    assert moved.status_code == 409
    assert moved.get_json()["code"] == "STARTED_LOCKED"
    kept = client.get("/api/shifts/S-01/jobs").get_json()["jobs"][0]
    assert kept["station_id"] == "ST1"
    assert kept["started_at"] == "2026-11-25T08:00:00"


def test_unstarted_job_with_receipt_gap_is_revoked(client):
    report = approved_report(client)
    make_shift(client)
    add_job(client, report, "J1", received_qty=8, report_qty=10, salvage_qty=8)
    add_job(client, report, "J2", expected_minutes=600, salvage_qty=10)
    job = client.get("/api/shifts/S-01/jobs").get_json()["jobs"][0]
    assert job["status"] == "REVOKED"
    plan = client.post("/api/shifts/S-01/schedule", json={}).get_json()["schedule"]
    station = next(item for item in plan["stations"] if item["station_id"] == "ST1")
    assert [item["job_id"] for item in station["items"]] == ["J2"]
    assert plan["totals"]["jobs_revoked"] == 1


def test_concurrent_schedules_do_not_oversubscribe(tmp_path):
    database = str(tmp_path / "concurrent.sqlite3")
    app = create_app(database)
    app.config.update(TESTING=True)
    client = app.test_client()
    report = approved_report(client)
    make_shift(client)
    for index in range(12):
        add_job(client, report, f"J{index:02d}", expected_minutes=100, salvage_qty=10)

    results = []
    barrier = threading.Barrier(7)

    def contender():
        local = app.test_client()
        barrier.wait()
        response = local.post("/api/shifts/S-01/schedule", json={"expected_revision": 0})
        results.append((response.status_code, response.get_json()))

    contenders = [threading.Thread(target=contender) for _ in range(6)]
    for thread in contenders:
        thread.start()
    barrier.wait()
    for thread in contenders:
        thread.join()

    ok = [item for item in results if item[0] == 200]
    stale = [item for item in results if item[0] == 409]
    assert len(ok) == 1
    assert all(item[1]["code"] == "STALE_SCHEDULE" for item in stale)
    assert len(ok) + len(stale) == 6
    final = client.get("/api/shifts/S-01/plan").get_json()
    for station in final["schedule"]["stations"]:
        assert station["used_minutes"] <= station["capacity_minutes"]
    # 全部工单只出现一次，且不重复占用
    scheduled = [item["job_id"] for station in final["schedule"]["stations"] for item in station["items"]]
    assert len(scheduled) == len(set(scheduled))


def test_schedule_is_idempotent_under_lock(client):
    report = approved_report(client)
    make_shift(client)
    for index in range(5):
        add_job(client, report, f"J{index}", expected_minutes=100, salvage_qty=10)
    first = client.post("/api/shifts/S-01/schedule", json={}).get_json()["schedule"]
    second = client.post("/api/shifts/S-01/schedule", json={}).get_json()["schedule"]
    strip = lambda plan: [{k: v for k, v in station.items()} for station in plan["stations"]]
    assert strip(first) == strip(second)


def test_review_and_evidence(client):
    report = approved_report(client)
    make_shift(client)
    add_job(client, report, "J1")
    plan = client.post("/api/shifts/S-01/schedule", json={}).get_json()
    before = client.get("/api/shifts/S-01/evidence")
    assert before.status_code == 409
    review = client.post("/api/shifts/S-01/review",
                         json={"decision": "APPROVED", "comment": "本班按此执行",
                               "expected_revision": plan["revision"]})
    assert review.status_code == 200
    evidence = client.get("/api/shifts/S-01/evidence")
    assert evidence.status_code == 200
    artifact = evidence.get_json()
    assert artifact["schema"] == "rework-schedule-evidence/v1"
    assert len(artifact["artifact_sha256"]) == 64
    # 重排让旧复核失效
    client.post("/api/shifts/S-01/schedule", json={})
    assert client.get("/api/shifts/S-01/evidence").status_code == 409


def test_stale_review_is_rejected(client):
    report = approved_report(client)
    make_shift(client)
    add_job(client, report, "J1")
    plan = client.post("/api/shifts/S-01/schedule", json={}).get_json()
    first = client.post("/api/shifts/S-01/review",
                        json={"decision": "APPROVED", "comment": "首次", "expected_revision": plan["revision"]})
    assert first.status_code == 200
    second = client.post("/api/shifts/S-01/review",
                         json={"decision": "APPROVED", "comment": "旧版", "expected_revision": plan["revision"]})
    assert second.status_code == 409
    assert second.get_json()["code"] == "STALE_SCHEDULE"


def test_plan_shows_started_order_and_deferred_reasons(client):
    report = approved_report(client)
    make_shift(client)
    add_job(client, report, "J1", started=True, started_at="2026-11-25T10:00:00", expected_minutes=60)
    add_job(client, report, "J2", expected_minutes=200, due_at="2026-11-25T09:00:00", salvage_qty=5)
    add_job(client, report, "J3", expected_minutes=1000, salvage_qty=9, due_at="2026-11-26T12:00:00")
    plan = client.post("/api/shifts/S-01/schedule", json={}).get_json()["schedule"]
    reasons = {item["job_id"]: item["reason"] for item in plan["deferred"]}
    assert reasons["J3"] == "CAPACITY"


def test_shift_change_invalidates_plan(client):
    report = approved_report(client)
    make_shift(client)
    add_job(client, report, "J1")
    plan = client.post("/api/shifts/S-01/schedule", json={}).get_json()
    changed = dict(SHIFT_BODY, stations=[{"station_id": "ST1", "name": "返工线1", "minutes": 120}],
                   expected_revision=plan["revision"])
    response = client.post("/api/shifts", json=changed)
    assert response.status_code == 200
    assert response.get_json()["schedule"] is None
    assert response.get_json()["review"] is None


def test_stale_shift_update_is_rejected(client):
    make_shift(client)
    first = client.post("/api/shifts", json=dict(SHIFT_BODY, expected_revision=0))
    assert first.status_code == 200
    second = client.post("/api/shifts", json=dict(SHIFT_BODY, expected_revision=0))
    assert second.status_code == 409
    assert second.get_json()["code"] == "STALE_SCHEDULE"


def test_review_requires_schedule(client):
    make_shift(client)
    response = client.post("/api/shifts/S-01/review",
                           json={"decision": "APPROVED", "comment": "x", "expected_revision": 0})
    assert response.status_code == 400


def test_shift_list_reports_state(client):
    report = approved_report(client)
    make_shift(client)
    add_job(client, report, "J1")
    listed = client.get("/api/shifts").get_json()["shifts"]
    assert listed[0]["shift_id"] == "S-01"
    assert listed[0]["scheduled"] is False
    client.post("/api/shifts/S-01/schedule", json={})
    assert client.get("/api/shifts").get_json()["shifts"][0]["scheduled"] is True


def test_started_job_cannot_be_unstarted_by_repost(client):
    report = approved_report(client)
    make_shift(client)
    add_job(client, report, "J1", started=True, started_at="2026-11-25T08:00:00",
            expected_minutes=60, salvage_qty=8)
    response = add_job(client, report, "J1", started=False)
    assert response.status_code == 409
    assert response.get_json()["code"] == "STARTED_LOCKED"


def test_started_job_quantity_can_be_corrected(client):
    report = approved_report(client)
    make_shift(client)
    add_job(client, report, "J1", started=True, started_at="2026-11-25T08:00:00",
            expected_minutes=60, salvage_qty=8)
    response = add_job(client, report, "J1", started=True, started_at="2026-11-25T08:00:00",
                       expected_minutes=90, salvage_qty=9)
    assert response.status_code == 200
    assert response.get_json()["expected_minutes"] == 90
    assert response.get_json()["station_id"] == "ST1"


def test_started_overflow_blocks_approval_but_plan_is_visible(client):
    report = approved_report(client)
    small = dict(SHIFT_BODY, stations=[{"station_id": "ST1", "name": "返工线1", "minutes": 60}])
    make_shift(client, small)
    add_job(client, report, "J1", started=True, started_at="2026-11-25T08:00:00",
            expected_minutes=120, salvage_qty=8)
    plan = client.post("/api/shifts/S-01/schedule", json={})
    assert plan.status_code == 200
    body = plan.get_json()["schedule"]
    assert body["totals"]["over_capacity"] is True
    review = client.post("/api/shifts/S-01/review",
                         json={"decision": "APPROVED", "comment": "尝试", "expected_revision": plan.get_json()["revision"]})
    assert review.status_code == 409
    assert review.get_json()["code"] == "GATE_BLOCKED"


def test_reports_endpoint_lists_only_approved(client):
    assert client.get("/api/reports").get_json()["reports"] == []
    report = approved_report(client)
    reports = client.get("/api/reports").get_json()["reports"]
    assert [item["run_id"] for item in reports] == [report]
    assert "WO1" in reports[0]["work_orders"]


def test_dirty_report_not_listed(client):
    client.post("/api/runs", json={"profile": "dirty"})
    assert client.get("/api/reports").get_json()["reports"] == []


def test_cannot_remove_station_with_started_job(client):
    report = approved_report(client)
    make_shift(client)
    add_job(client, report, "J1", started=True, started_at="2026-11-25T08:00:00",
            expected_minutes=60, salvage_qty=8)
    changed = dict(SHIFT_BODY, stations=[{"station_id": "ST2", "name": "返工线2", "minutes": 600}])
    response = client.post("/api/shifts", json=changed)
    assert response.status_code == 409
    assert response.get_json()["code"] == "STARTED_LOCKED"
    # 原班次与开工记录保持不变
    shift = client.get("/api/shifts/S-01").get_json()
    assert {station["station_id"] for station in shift["stations"]} == {"ST1", "ST2"}
    assert client.get("/api/shifts/S-01/jobs").get_json()["jobs"][0]["started"] is True
