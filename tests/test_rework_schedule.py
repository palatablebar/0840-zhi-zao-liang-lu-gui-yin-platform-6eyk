from app.errors import DomainIssue
from app.rework.schedule import build_plan, knapsack

import pytest


SHIFT = {
    "shift_id": "S1",
    "starts_at": "2026-11-25T08:00:00",
    "ends_at": "2026-11-25T18:00:00",
    "stations": [{"station_id": "ST1", "name": "返工线1", "minutes": 600}],
}


def job(job_id, **changes):
    item = {
        "job_id": job_id,
        "report_run_id": "RUN1",
        "station_id": "ST1",
        "work_order": "WO" + job_id,
        "expected_minutes": 60,
        "due_at": "2026-11-25T17:00:00",
        "salvage_qty": 10,
        "report_qty": 10,
        "received_qty": 10,
        "started": False,
        "started_at": None,
        "urgent": None,
    }
    item.update(changes)
    return item


def schedule(jobs, shift=None):
    return build_plan(shift or SHIFT, jobs, revision=1)


def test_capacity_is_never_exceeded():
    plan = schedule([job(str(i), expected_minutes=100) for i in range(10)])
    for station in plan["stations"]:
        assert station["used_minutes"] <= station["capacity_minutes"]
    assert plan["totals"]["used_minutes"] == 600


def test_started_job_keeps_its_station_and_slot():
    locked = job("A", started=True, started_at="2026-11-25T09:30:00", expected_minutes=60)
    plan = schedule([locked, job("B", expected_minutes=60)])
    station = plan["stations"][0]
    first = station["items"][0]
    assert first["job_id"] == "A"
    assert first["role"] == "STARTED"
    assert first["start_at"] == "2026-11-25T09:30:00"
    assert first["end_at"] == "2026-11-25T10:30:00"


def test_started_job_cannot_be_moved_to_other_station():
    bad = job("A", started=True, started_at="2026-11-25T09:00:00", station_id="NOPE")
    with pytest.raises(DomainIssue) as caught:
        schedule([bad])
    assert caught.value.code == "STATION_NOT_FOUND"


def test_urgent_whole_batch_is_preferred_over_higher_salvage():
    # 急单交期 10:00，必须在它之前完成；另一单救料更多但交期晚，不能抢占急单。
    urgent = job("U", expected_minutes=60, due_at="2026-11-25T10:00:00", salvage_qty=5)
    bulk = job("B", expected_minutes=600, due_at="2026-11-25T18:00:00", salvage_qty=500)
    plan = schedule([urgent, bulk], shift=dict(SHIFT, stations=[{"station_id": "ST1", "name": "x", "minutes": 600}]))
    station = plan["stations"][0]
    assert station["items"][0]["job_id"] == "U"
    assert station["items"][0]["role"] == "URGENT"
    assert station["items"][0]["on_time"] is True
    # 急单只救 5 件，却占掉 60 分钟；余下 540 分钟装不下 600 分钟的整单，宁可少量救料也要保急单。
    assert plan["totals"]["salvaged"] == 5
    assert [item["job_id"] for item in plan["deferred"]] == ["B"]


def test_urgent_that_cannot_arrive_whole_batch_is_not_prioritised():
    # 08:00 开工、90 分钟最早 09:30 完成，赶不上 09:00；不再占用急单位，退回余量池。
    urgent = job("U", expected_minutes=90, due_at="2026-11-25T09:00:00", salvage_qty=50)
    plan = schedule([urgent])
    item = plan["stations"][0]["items"][0]
    assert item["role"] == "SALVAGE"
    assert item["on_time"] is False


def test_remaining_capacity_maximises_salvaged_pieces():
    # 600 分钟装不下 300+300+200；A+C 能救 290 件，A+B 只有 200 件。
    jobs = [
        job("A", expected_minutes=300, salvage_qty=100, due_at="2026-11-26T12:00:00"),
        job("B", expected_minutes=300, salvage_qty=100, due_at="2026-11-26T12:00:00"),
        job("C", expected_minutes=200, salvage_qty=190, due_at="2026-11-26T12:00:00"),
    ]
    plan = schedule(jobs)
    chosen = {item["job_id"] for item in plan["stations"][0]["items"]}
    assert chosen == {"A", "C"}
    assert plan["totals"]["salvaged"] == 290


def test_greedy_by_salvage_would_lose_to_packing():
    # 单看救料 X 最多，但 X 独占整班；Y+Z 合计更多，背包应选组合。
    jobs = [
        job("X", expected_minutes=600, salvage_qty=100, due_at="2026-11-26T12:00:00"),
        job("Y", expected_minutes=400, salvage_qty=90, due_at="2026-11-26T12:00:00"),
        job("Z", expected_minutes=200, salvage_qty=90, due_at="2026-11-26T12:00:00"),
    ]
    plan = schedule(jobs)
    assert plan["totals"]["salvaged"] == 180
    assert {item["job_id"] for item in plan["stations"][0]["items"]} == {"Y", "Z"}


def test_due_order_breaks_ties_for_remaining_jobs():
    jobs = [
        job("LATE", expected_minutes=100, salvage_qty=10, due_at="2026-11-26T12:00:00"),
        job("EARLY", expected_minutes=100, salvage_qty=10, due_at="2026-11-25T12:00:00"),
    ]
    plan = schedule(jobs)
    order = [item["job_id"] for item in plan["stations"][0]["items"]]
    assert order == ["EARLY", "LATE"]


def test_incomplete_job_is_not_scheduled():
    plan = schedule([job("A", due_at=None)])
    assert plan["totals"]["jobs_scheduled"] == 0
    assert plan["deferred"][0]["reason"] == "INCOMPLETE"


def test_revoked_job_releases_capacity():
    revoked = job("A", received_qty=8, report_qty=10)
    plan = schedule([revoked, job("B", expected_minutes=600)])
    assert plan["totals"]["jobs_revoked"] == 1
    assert plan["stations"][0]["items"][0]["job_id"] == "B"
    assert any(item["reason"] == "RECEIPT_MISMATCH" for item in plan["deferred"])


def test_started_job_is_not_revoked_by_receipt_mismatch():
    locked = job("A", started=True, started_at="2026-11-25T08:00:00", received_qty=2, report_qty=10)
    plan = schedule([locked])
    assert plan["totals"]["jobs_revoked"] == 0
    assert plan["stations"][0]["items"][0]["role"] == "STARTED"
    assert any(item["code"] == "STARTED_MISMATCH" for item in plan["issues"])


def test_started_overflow_reports_capacity_issue():
    locked = job("A", started=True, started_at="2026-11-25T17:30:00", expected_minutes=60)
    plan = schedule([locked], shift=dict(SHIFT, stations=[{"station_id": "ST1", "name": "x", "minutes": 600}]))
    assert any(item["code"] == "STARTED_OVER_CAPACITY" for item in plan["issues"])
    assert plan["totals"]["over_capacity"] is True
    # 超占仍如实落排，工位不被静默截断
    assert plan["stations"][0]["items"][0]["job_id"] == "A"


def test_started_minutes_beyond_capacity_blocks_approval():
    locked = job("A", started=True, started_at="2026-11-25T08:00:00", expected_minutes=700)
    plan = schedule([locked], shift=dict(SHIFT, stations=[{"station_id": "ST1", "name": "x", "minutes": 600}]))
    assert any(item["code"] == "CAPACITY_OVERFLOW" for item in plan["issues"])
    assert plan["totals"]["over_capacity"] is True


def test_knapsack_selects_by_value_then_lighter_weight():
    items = [{"expected_minutes": 30, "salvage_qty": 10}, {"expected_minutes": 20, "salvage_qty": 10}]
    assert knapsack(items, 20) == [1]


def test_started_overlap_is_flagged_not_shifted():
    first = job("A", started=True, started_at="2026-11-25T09:00:00", expected_minutes=120)
    second = job("B", started=True, started_at="2026-11-25T09:30:00", expected_minutes=30)
    plan = schedule([first, second])
    starts = {item["job_id"]: item["start_at"] for item in plan["stations"][0]["items"]}
    assert starts["A"] == "2026-11-25T09:00:00"
    assert starts["B"] == "2026-11-25T09:30:00"
    assert any(item["code"] == "STARTED_OVERLAP" for item in plan["issues"])


def test_started_before_shift_is_clamped_and_flagged():
    early = job("A", started=True, started_at="2026-11-25T06:00:00", expected_minutes=60)
    plan = schedule([early])
    item = plan["stations"][0]["items"][0]
    assert item["start_at"] == "2026-11-25T08:00:00"
    assert any(issue["code"] == "STARTED_BEFORE_SHIFT" for issue in plan["issues"])


def test_unassigned_job_is_deferred():
    plan = schedule([job("A", station_id=None)])
    assert plan["totals"]["jobs_scheduled"] == 0
    assert plan["deferred"][0]["reason"] == "NO_STATION"


def test_explicit_urgent_flag_overrides_due():
    late_due = job("U", urgent=True, expected_minutes=60, due_at="2026-11-25T16:00:00", salvage_qty=1)
    plan = schedule([late_due])
    assert plan["stations"][0]["items"][0]["role"] == "URGENT"


def test_empty_station_stays_idle():
    shift = {"shift_id": "S1", "starts_at": "2026-11-25T08:00:00", "ends_at": "2026-11-25T18:00:00",
             "stations": [{"station_id": "S1", "minutes": 600}, {"station_id": "S2", "minutes": 300}]}
    plan = build_plan(shift, [job("A")], revision=1)
    empty = next(station for station in plan["stations"] if station["station_id"] == "S2")
    assert empty["items"] == []
    assert empty["idle_minutes"] == 300
