from app.evidence.manifest import stable_digest


def render_shift(row, jobs):
    body = row["body"]
    artifact = {
        "schema": "rework-schedule-evidence/v1",
        "shift_id": row["shift_id"],
        "starts_at": body["starts_at"],
        "ends_at": body["ends_at"],
        "stations": body["stations"],
        "revision": row["revision"],
        "schedule": row["schedule"],
        "review": row["review"],
        "jobs": sorted(
            ({"job_id": job["job_id"], "report_run_id": job.get("report_run_id"),
              "station_id": job.get("station_id"), "expected_minutes": job.get("expected_minutes"),
              "due_at": job.get("due_at"), "salvage_qty": job.get("salvage_qty"),
              "started": bool(job.get("started")), "started_at": job.get("started_at")}
             for job in jobs),
            key=lambda job: job["job_id"]),
    }
    artifact["artifact_sha256"] = stable_digest(artifact)
    return artifact
