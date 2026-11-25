import json
from datetime import datetime

from app.errors import DomainIssue


def _moment(value, code="BAD_MOMENT"):
    try:
        return datetime.fromisoformat(str(value)).replace(microsecond=0)
    except (TypeError, ValueError):
        raise DomainIssue(code, 400)


def _int(value, code, positive=False):
    if isinstance(value, bool):
        raise DomainIssue(code, 400)
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise DomainIssue(code, 400)
    if number < 1 if positive else number < 0:
        raise DomainIssue(code, 400)
    return number


def validate_shift(payload):
    if not payload.get("shift_id"):
        raise DomainIssue("BAD_REQUEST", 400, detail="缺少班次编号")
    starts_at = _moment(payload.get("starts_at"))
    ends_at = _moment(payload.get("ends_at"))
    if ends_at <= starts_at:
        raise DomainIssue("BAD_REQUEST", 400, detail="班次结束必须晚于开始")
    stations = payload.get("stations") or []
    if not stations:
        raise DomainIssue("BAD_REQUEST", 400, detail="至少需要一个工位")
    seen = set()
    clean = []
    for station in stations:
        station_id = str(station.get("station_id") or "").strip()
        if not station_id or station_id in seen:
            raise DomainIssue("BAD_REQUEST", 400, detail="工位编号为空或重复")
        seen.add(station_id)
        clean.append({
            "station_id": station_id,
            "name": str(station.get("name") or station_id),
            "minutes": _int(station.get("minutes"), "BAD_MINUTES", positive=True),
        })
    return {
        "shift_id": str(payload["shift_id"]).strip(),
        "starts_at": starts_at.isoformat(timespec="seconds"),
        "ends_at": ends_at.isoformat(timespec="seconds"),
        "stations": clean,
    }


def _optional_int(value, code):
    if value is None or str(value).strip() == "":
        return None
    return _int(value, code)


def validate_job(payload, shift):
    """整理返工单：补录预计工时、交期、可救件数，并落实实收对账与已开工锁定。"""
    job_id = str(payload.get("job_id") or "").strip()
    if not job_id:
        raise DomainIssue("BAD_REQUEST", 400, detail="缺少返工单号")
    report_run_id = str(payload.get("report_run_id") or "").strip()
    if not report_run_id:
        raise DomainIssue("BAD_REQUEST", 400, detail="缺少报工批次")
    station_ids = {station["station_id"] for station in shift["stations"]}
    station_id = str(payload.get("station_id") or "").strip() or None
    if station_id is not None and station_id not in station_ids:
        raise DomainIssue("STATION_NOT_FOUND", 400, detail="工位不在本班次")

    started = payload.get("started") in (True, "true", "True", 1, "1")
    started_at = payload.get("started_at")
    if started:
        started_at = _moment(started_at).isoformat(timespec="seconds")
        if station_id is None:
            raise DomainIssue("STARTED_JOB_INCOMPLETE", 400, detail="已开工返工缺少工位")
        if _optional_int(payload.get("expected_minutes"), "BAD_MINUTES") is None:
            raise DomainIssue("STARTED_JOB_INCOMPLETE", 400, detail="已开工返工缺少预计工时")
    else:
        started_at = None

    expected_minutes = _optional_int(payload.get("expected_minutes"), "BAD_MINUTES")
    if expected_minutes is not None and expected_minutes <= 0:
        raise DomainIssue("BAD_MINUTES", 400)

    due_at = None
    if payload.get("due_at") not in (None, ""):
        due_at = _moment(payload.get("due_at")).isoformat(timespec="seconds")

    report_qty = _optional_int(payload.get("report_qty"), "BAD_QUANTITY")
    salvage_qty = _optional_int(payload.get("salvage_qty"), "BAD_QUANTITY")
    received_qty = _optional_int(payload.get("received_qty"), "BAD_QUANTITY")
    if salvage_qty is not None:
        if report_qty is not None and salvage_qty > report_qty:
            raise DomainIssue("SALVAGE_OVER_REPORT", 400)
        if received_qty is not None and salvage_qty > received_qty:
            raise DomainIssue("SALVAGE_OVER_RECEIPT", 400)

    urgent = payload.get("urgent")
    if urgent in ("", None):
        urgent = None
    elif isinstance(urgent, str):
        urgent = urgent.strip().lower() in {"1", "true", "yes", "urgent"}
    else:
        urgent = bool(urgent)

    return {
        "job_id": job_id,
        "shift_id": shift["shift_id"],
        "report_run_id": report_run_id,
        "work_order": str(payload.get("work_order") or ""),
        "station_id": station_id,
        "expected_minutes": expected_minutes,
        "due_at": due_at,
        "urgent": urgent,
        "salvage_qty": salvage_qty,
        "report_qty": report_qty,
        "received_qty": received_qty,
        "started": started,
        "started_at": started_at,
    }


def report_is_approved(run_store, report_run_id, work_order=None):
    """报工必须已签发、无阻断项，且返工单所属工单确实在报工结果里，才能排返工。"""
    row = run_store.get(report_run_id)
    if row is None:
        raise DomainIssue("REPORT_NOT_APPROVED", 409, detail="报工批次 %s 不存在" % report_run_id)
    if row["status"] != "APPROVED":
        raise DomainIssue("REPORT_NOT_APPROVED", 409, detail="报工批次 %s 尚未签发" % report_run_id)
    payload = json.loads(row["payload"])
    if int(payload.get("summary", {}).get("REJECT", 0)) != 0:
        raise DomainIssue("REPORT_NOT_APPROVED", 409, detail="报工批次 %s 存在阻断项" % report_run_id)
    if work_order:
        labels = {str(item.get("label")) for item in payload.get("rows", [])}
        if labels and work_order not in labels:
            raise DomainIssue("REPORT_NOT_APPROVED", 409,
                              detail="工单 %s 不在报工批次 %s 内" % (work_order, report_run_id))
    return payload


def job_status(job, stations):
    """返工单能否进入排程，以及是否已撤销占用。

    已开工返工永不撤销——开工事实不可回滚，实收差异只升级为人工核对。
    """
    if job.get("started"):
        return "READY" if job.get("station_id") in {item["station_id"] for item in stations} else "UNASSIGNED"
    if job.get("received_qty") is not None and job.get("report_qty") is not None \
            and job.get("received_qty") != job.get("report_qty"):
        return "REVOKED"
    if job.get("expected_minutes") is None or job.get("due_at") is None or job.get("salvage_qty") is None:
        return "INCOMPLETE"
    if job.get("station_id") not in {item["station_id"] for item in stations}:
        return "UNASSIGNED"
    return "READY"


def normalize(job, shift):
    """把派生的撤销/待补录状态挂到返工单视图上（撤销来自实收对账，不单独存盘）。"""
    return dict(job, status=job_status(job, shift["stations"]))
