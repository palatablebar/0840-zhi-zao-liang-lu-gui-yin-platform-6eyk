import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from app.errors import DomainIssue


SCHEMA = '''
CREATE TABLE IF NOT EXISTS shifts(
  id TEXT PRIMARY KEY,
  payload TEXT NOT NULL,
  revision INTEGER NOT NULL DEFAULT 0,
  schedule TEXT,
  review TEXT,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs(
  id TEXT PRIMARY KEY,
  shift_id TEXT NOT NULL,
  payload TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
'''

# expected_revision 与 CURRENT 相同表示“不校验”；排程与复核都会比对版本号，
# 两个请求同时提交时只有一个能拿到同一基础版本，另一个收到 STALE_SCHEDULE。
SKIP_REVISION = object()


class ReworkStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init()

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=8, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
        except BaseException:
            db.execute("ROLLBACK")
            raise
        else:
            db.execute("COMMIT")
        finally:
            db.close()

    @contextmanager
    def reading(self):
        db = sqlite3.connect(self.path, timeout=8)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    def init(self):
        with self.reading() as db:
            db.executescript(SCHEMA)
            db.commit()

    # ---- 班次 ----

    def get_shift(self, shift_id):
        with self.reading() as db:
            row = db.execute("SELECT * FROM shifts WHERE id=?", (shift_id,)).fetchone()
        return _shift_row(row)

    def list_shifts(self):
        with self.reading() as db:
            rows = db.execute("SELECT * FROM shifts ORDER BY id").fetchall()
        return [_shift_row(row) for row in rows]

    def put_shift(self, shift, expected_revision=SKIP_REVISION):
        now = _now()
        body = {key: shift[key] for key in ("starts_at", "ends_at", "stations")}
        with self.transaction() as db:
            row = db.execute("SELECT revision FROM shifts WHERE id=?", (shift["shift_id"],)).fetchone()
            if row is None:
                db.execute(
                    "INSERT INTO shifts(id,payload,revision,schedule,review,updated_at) VALUES(?,?,0,NULL,NULL,?)",
                    (shift["shift_id"], json.dumps(body, ensure_ascii=False), now))
            else:
                if expected_revision is not SKIP_REVISION and int(expected_revision) != row["revision"]:
                    raise DomainIssue("STALE_SCHEDULE", 409)
                known = {station["station_id"] for station in body["stations"]}
                for item in db.execute("SELECT payload FROM jobs WHERE shift_id=?", (shift["shift_id"],)).fetchall():
                    job = json.loads(item["payload"])
                    if job.get("started") and job.get("station_id") not in known:
                        raise DomainIssue("STARTED_LOCKED", 409,
                                          detail="已开工返工 %s 固定在该工位，不能从班次移除" % job["job_id"])
                # 班次工时/起止变了，旧排程与旧复核立即失效，必须重排。
                db.execute(
                    "UPDATE shifts SET payload=?, schedule=NULL, review=NULL, revision=revision+1, updated_at=? "
                    "WHERE id=?", (json.dumps(body, ensure_ascii=False), now, shift["shift_id"]))
        return self.get_shift(shift["shift_id"])

    def reschedule(self, shift_id, expected_revision=SKIP_REVISION):
        """在同一写事务里读返工单、算排程、落库。

        两个排程请求同时提交时，事务串行化：后一个拿到的是已推进的 revision，
        若还带着旧 expected_revision 会被 STALE_SCHEDULE 拒绝，绝不超占工位。
        """
        from app.rework.schedule import build_plan
        with self.transaction() as db:
            row = db.execute("SELECT * FROM shifts WHERE id=?", (shift_id,)).fetchone()
            if row is None:
                raise DomainIssue("SHIFT_NOT_FOUND", 404)
            current = row["revision"]
            if expected_revision is not SKIP_REVISION and int(expected_revision) != current:
                raise DomainIssue("STALE_SCHEDULE", 409)
            body = dict(json.loads(row["payload"]), shift_id=shift_id)
            jobs = [dict(json.loads(item["payload"]), job_id=item["id"]) for item in
                    db.execute("SELECT * FROM jobs WHERE shift_id=? ORDER BY id", (shift_id,)).fetchall()]
            plan = build_plan(body, jobs, revision=current + 1)
            db.execute(
                "UPDATE shifts SET schedule=?, revision=revision+1, review=NULL, updated_at=? WHERE id=?",
                (json.dumps(plan, ensure_ascii=False), _now(), shift_id))
            row = db.execute("SELECT * FROM shifts WHERE id=?", (shift_id,)).fetchone()
            return _shift_row(row)

    def set_review(self, shift_id, review, expected_revision=SKIP_REVISION):
        with self.transaction() as db:
            row = db.execute("SELECT revision, schedule FROM shifts WHERE id=?", (shift_id,)).fetchone()
            if row is None:
                raise DomainIssue("SHIFT_NOT_FOUND", 404)
            if row["schedule"] is None:
                raise DomainIssue("BAD_REQUEST", 400, detail="尚未排程，不能复核")
            if expected_revision is not SKIP_REVISION and int(expected_revision) != row["revision"]:
                raise DomainIssue("STALE_SCHEDULE", 409)
            db.execute("UPDATE shifts SET review=?, revision=revision+1, updated_at=? WHERE id=?",
                       (json.dumps(review, ensure_ascii=False), _now(), shift_id))
        return self.get_shift(shift_id)

    # ---- 返工单 ----

    def list_jobs(self, shift_id):
        with self.reading() as db:
            rows = db.execute("SELECT * FROM jobs WHERE shift_id=? ORDER BY id", (shift_id,)).fetchall()
        return [dict(json.loads(row["payload"]), job_id=row["id"]) for row in rows]

    def get_job(self, job_id):
        with self.reading() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return None
        return dict(json.loads(row["payload"]), job_id=row["id"])

    def put_job(self, job):
        now = _now()
        with self.transaction() as db:
            row = db.execute("SELECT id, shift_id FROM jobs WHERE id=?", (job["job_id"],)).fetchone()
            if row is not None and row["shift_id"] != job["shift_id"]:
                raise DomainIssue("JOB_EXISTS", 409, detail="返工单 %s 已属于其它班次" % job["job_id"])
            body = json.dumps(job, ensure_ascii=False)
            if row is None:
                db.execute("INSERT INTO jobs(id,shift_id,payload,updated_at) VALUES(?,?,?,?)",
                           (job["job_id"], job["shift_id"], body, now))
            else:
                db.execute("UPDATE jobs SET payload=?, updated_at=? WHERE id=?", (body, now, job["job_id"]))
        return self.get_job(job["job_id"])

    def delete_job(self, job_id):
        with self.transaction() as db:
            db.execute("DELETE FROM jobs WHERE id=?", (job_id,))


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _shift_row(row):
    if row is None:
        return None
    return {
        "shift_id": row["id"],
        "revision": row["revision"],
        "updated_at": row["updated_at"],
        "schedule": json.loads(row["schedule"]) if row["schedule"] else None,
        "review": json.loads(row["review"]) if row["review"] else None,
        "body": json.loads(row["payload"]),
    }
