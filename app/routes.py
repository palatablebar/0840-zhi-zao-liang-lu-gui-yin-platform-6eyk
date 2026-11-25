from flask import Blueprint, abort, jsonify, request, send_file

from app.catalog import SPEC
from app.config import SAMPLES
from app.errors import DomainIssue
from app.evidence.render import render
from app.review.service import approve, create_run, serialize
from app.rework.service import normalize, report_is_approved, validate_job, validate_shift
from app.rework.store import SKIP_REVISION
from app.service import pipeline


api = Blueprint("api", __name__)
STAGE_INDEX = {"intake": 1, "prepare": 2, "calculate": 3, "decision": 4, "reconcile": 5}


def store():
    from flask import current_app
    return current_app.extensions["runs"]


def rework_store():
    from flask import current_app
    return current_app.extensions["rework"]


@api.get("/api/catalog")
def catalog():
    return jsonify(code=SPEC["code"], name=SPEC["name"], actor=SPEC["actor"], rule=SPEC["rule"],
                   rule_short=SPEC["rule_short"],
                   stages=[{"id": item, "name": name} for item, name in zip(
                       ["intake", "prepare", "calculate", "decision", "reconcile", "review", "evidence"],
                       SPEC["stages"])],
                   chart=SPEC["chart"], result_columns=SPEC["result_columns"],
                   columns=SPEC["columns"], samples=["dirty", "clean"])


@api.post("/api/stages/<stage_id>")
def stage(stage_id):
    if stage_id not in STAGE_INDEX:
        raise DomainIssue("STAGE_UNKNOWN", 404)
    data = request.get_json(silent=True) or {}
    profile = data.get("profile", "dirty")
    upto = SPEC["stages"][STAGE_INDEX[stage_id] - 1]
    return jsonify(pipeline(profile, upto=upto))


@api.post("/api/runs")
def create():
    data = request.get_json(silent=True) or {}
    profile = data.get("profile", "dirty")
    payload = pipeline(profile)
    return jsonify(create_run(store(), payload))


@api.get("/api/runs/<run_id>")
def get_run(run_id):
    run = serialize(store().get(run_id))
    if not run:
        raise DomainIssue("RUN_NOT_FOUND", 404)
    return jsonify(run)


@api.post("/api/runs/<run_id>/review")
def review(run_id):
    data = request.get_json(silent=True) or {}
    if "decision" not in data or "expected_revision" not in data:
        raise DomainIssue("BAD_REQUEST")
    return jsonify(approve(store(), run_id, data["decision"], data.get("comment", ""), int(data["expected_revision"])))


@api.get("/api/runs/<run_id>/evidence")
def evidence(run_id):
    run = serialize(store().get(run_id))
    return jsonify(render(run))


@api.get("/api/examples/<profile>")
def example(profile):
    if profile not in {"clean", "dirty"}:
        abort(404)
    return send_file(SAMPLES / f"{profile}.csv", as_attachment=True)


# ---- 返工排程 ----

@api.post("/api/shifts")
def upsert_shift():
    data = request.get_json(silent=True) or {}
    body = validate_shift(data)
    expected = int(data["expected_revision"]) if "expected_revision" in data else SKIP_REVISION
    row = rework_store().put_shift(body, expected)
    return jsonify(_shift_view(row, rework_store().list_jobs(body["shift_id"])))


@api.get("/api/reports")
def list_reports():
    """可供排返工的已签发报工批次。"""
    reports = []
    for row in store().list_approved():
        payload = row["payload"]
        if int(payload.get("summary", {}).get("REJECT", 0)) != 0:
            continue
        reports.append({
            "run_id": row["run_id"],
            "profile": row["profile"],
            "revision": row["revision"],
            "work_orders": [item.get("label") for item in payload.get("rows", [])],
        })
    return jsonify(reports=reports)


@api.get("/api/shifts")
def list_shifts():
    rows = rework_store().list_shifts()
    return jsonify(shifts=[{
        "shift_id": row["shift_id"],
        "starts_at": row["body"]["starts_at"],
        "ends_at": row["body"]["ends_at"],
        "revision": row["revision"],
        "reviewed": bool(row["review"] and row["review"].get("decision") == "APPROVED"),
        "scheduled": row["schedule"] is not None,
    } for row in rows])


@api.get("/api/shifts/<shift_id>")
def get_shift(shift_id):
    row = rework_store().get_shift(shift_id)
    if row is None:
        raise DomainIssue("SHIFT_NOT_FOUND", 404)
    return jsonify(_shift_view(row, rework_store().list_jobs(shift_id)))


@api.post("/api/shifts/<shift_id>/jobs")
def put_job(shift_id):
    data = request.get_json(silent=True) or {}
    shift = rework_store().get_shift(shift_id)
    if shift is None:
        raise DomainIssue("SHIFT_NOT_FOUND", 404)
    job = validate_job(data, dict(shift["body"], shift_id=shift_id))
    existing = rework_store().get_job(job["job_id"])
    if existing is not None and existing.get("started"):
        # 已开工的返工不可挪动、不可撤回开工；补录只允许改数量类字段。
        moved = (job.get("station_id") not in (None, existing.get("station_id"))
                 or job.get("started_at") not in (None, existing.get("started_at"))
                 or not job.get("started"))
        if moved:
            raise DomainIssue("STARTED_LOCKED", 409,
                              detail="返工单 %s 已开工，不能挪动工位、开工时刻或撤回开工" % job["job_id"])
        job = dict(existing, expected_minutes=job["expected_minutes"], due_at=job["due_at"],
                   salvage_qty=job["salvage_qty"], report_qty=job["report_qty"],
                   received_qty=job["received_qty"], urgent=job["urgent"])
    if job.get("report_run_id"):
        report_is_approved(store(), job["report_run_id"], job.get("work_order"))
    saved = rework_store().put_job(job)
    return jsonify(normalize(saved, shift["body"]))


@api.get("/api/shifts/<shift_id>/jobs")
def list_jobs(shift_id):
    shift = rework_store().get_shift(shift_id)
    if shift is None:
        raise DomainIssue("SHIFT_NOT_FOUND", 404)
    jobs = [normalize(job, shift["body"]) for job in rework_store().list_jobs(shift_id)]
    jobs.sort(key=lambda job: job["job_id"])
    return jsonify(shift_id=shift_id, revision=shift["revision"], jobs=jobs)


@api.post("/api/shifts/<shift_id>/jobs/<job_id>/start")
def start_job(shift_id, job_id):
    data = request.get_json(silent=True) or {}
    shift = rework_store().get_shift(shift_id)
    if shift is None:
        raise DomainIssue("SHIFT_NOT_FOUND", 404)
    existing = rework_store().get_job(job_id)
    if existing is None or existing["shift_id"] != shift_id:
        raise DomainIssue("JOB_NOT_FOUND", 404)
    if existing.get("started"):
        raise DomainIssue("STARTED_LOCKED", 409, detail="返工单 %s 已开工，不能挪动工位或开工时刻" % job_id)
    merged = dict(existing)
    merged.update(started=True, station_id=data.get("station_id") or existing.get("station_id"),
                  started_at=data.get("started_at") or existing.get("started_at"))
    if data.get("expected_minutes") is not None:
        merged["expected_minutes"] = data["expected_minutes"]
    job = validate_job(merged, dict(shift["body"], shift_id=shift_id))
    saved = rework_store().put_job(job)
    return jsonify(normalize(saved, shift["body"]))


@api.post("/api/shifts/<shift_id>/schedule")
def schedule_shift(shift_id):
    data = request.get_json(silent=True) or {}
    expected = int(data["expected_revision"]) if "expected_revision" in data else SKIP_REVISION
    row = rework_store().reschedule(shift_id, expected)
    return jsonify(dict(row["body"], revision=row["revision"], schedule=row["schedule"],
                        review=row["review"], updated_at=row["updated_at"]))


@api.get("/api/shifts/<shift_id>/plan")
def read_plan(shift_id):
    shift = rework_store().get_shift(shift_id)
    if shift is None:
        raise DomainIssue("SHIFT_NOT_FOUND", 404)
    jobs = rework_store().list_jobs(shift_id)
    payload = dict(shift["body"], revision=shift["revision"], schedule=shift["schedule"],
                   review=shift["review"], updated_at=shift["updated_at"])
    payload["jobs"] = [normalize(job, shift["body"]) for job in jobs]
    return jsonify(payload)


@api.post("/api/shifts/<shift_id>/review")
def review_shift(shift_id):
    data = request.get_json(silent=True) or {}
    if data.get("decision") not in {"APPROVED", "HELD"} or "expected_revision" not in data:
        raise DomainIssue("BAD_REQUEST")
    row = rework_store().get_shift(shift_id)
    if row is None:
        raise DomainIssue("SHIFT_NOT_FOUND", 404)
    if row["schedule"] is None:
        raise DomainIssue("BAD_REQUEST", 400, detail="尚未排程，不能复核")
    if data["decision"] == "APPROVED" and row["schedule"].get("totals", {}).get("over_capacity"):
        raise DomainIssue("GATE_BLOCKED", 409, detail="存在超占工位，不能签发")
    review = {"decision": data["decision"], "comment": str(data.get("comment") or "").strip(),
              "revision": row["revision"], "schedule_revision": row["schedule"]["revision"]}
    row = rework_store().set_review(shift_id, review, int(data["expected_revision"]))
    return jsonify(dict(row["body"], revision=row["revision"], schedule=row["schedule"], review=row["review"],
                        updated_at=row["updated_at"]))


@api.get("/api/shifts/<shift_id>/evidence")
def shift_evidence(shift_id):
    from app.rework.evidence import render_shift
    row = rework_store().get_shift(shift_id)
    if row is None:
        raise DomainIssue("SHIFT_NOT_FOUND", 404)
    if row["review"] is None or row["review"]["decision"] != "APPROVED":
        raise DomainIssue("NOT_APPROVED", 409)
    jobs = rework_store().list_jobs(shift_id)
    return jsonify(render_shift(row, jobs))


def _shift_view(row, jobs):
    body = row["body"]
    return dict(body, revision=row["revision"], schedule=row["schedule"], review=row["review"],
                updated_at=row["updated_at"],
                jobs=[normalize(job, body) for job in jobs])
