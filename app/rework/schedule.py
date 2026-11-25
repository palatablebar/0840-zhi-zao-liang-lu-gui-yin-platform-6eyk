"""返工排程的纯领域逻辑。

排程目标（见 docs/REWORK.md）：

1. 已开工的返工固定在原工位、原顺序，先占用该工位工时，不允许挪动。
2. 未开工但实收对账有差异的返工已被撤销占用，不参与排程。
3. 急单（交期落在本班内，或显式标记）按交期先后整批试排：
   只有整批能在本工位剩余工时内、且在交期前完成的才优先占用。
4. 剩余工时按 0/1 背包尽量多救料（整单排入，不拆单），再用交期顺序落位。
5. 同一工位的已排工时不得超过该工位可用工时。
"""
from datetime import datetime, timedelta

from app.errors import DomainIssue
from app.rework.service import job_status


ROLE_STARTED = "STARTED"
ROLE_URGENT = "URGENT"
ROLE_SALVAGE = "SALVAGE"


def _at(value):
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _plus(start, minutes):
    return start + timedelta(minutes=minutes)


def _due(value):
    return None if value in (None, "") else _at(value)


def is_complete(job):
    return (job.get("expected_minutes") is not None
            and job.get("due_at") not in (None, "")
            and job.get("salvage_qty") is not None)


def is_urgent(job, shift):
    if job.get("urgent") is not None:
        return bool(job["urgent"])
    due = _due(job.get("due_at"))
    return due is not None and due <= _at(shift["ends_at"])


def knapsack(items, capacity):
    """0/1 背包：在 capacity 分钟内使救料件数最大，返回被选中的下标列表。

    同值优先较早交期（items 已按交期、工单号排序后传入），再以较小位掩码稳定裁决。
    """
    capacity = int(capacity)
    if capacity <= 0 or not items:
        return []
    total = sum(int(item["expected_minutes"]) for item in items)
    if total <= capacity:
        return list(range(len(items)))
    # states: 重量 -> (价值, 位掩码)，只保留帕累托前沿
    states = {0: (0, 0)}
    for index, item in enumerate(items):
        weight = int(item["expected_minutes"])
        value = int(item["salvage_qty"] or 0)
        if weight <= 0:
            continue
        merged = dict(states)
        for used, (score, mask) in states.items():
            next_used = used + weight
            if next_used > capacity:
                continue
            candidate = (score + value, mask | (1 << index))
            current = merged.get(next_used)
            if current is None or candidate[0] > current[0] or (
                    candidate[0] == current[0] and candidate[1] < current[1]):
                merged[next_used] = candidate
        # 支配剪枝：重量更大且价值不更高的状态永远不会更优
        frontier, best = {}, -1
        for used in sorted(merged):
            score, mask = merged[used]
            if score > best:
                frontier[used] = (score, mask)
                best = score
        states = frontier
    best_used, (best_score, best_mask) = min(
        states.items(), key=lambda pair: (-pair[1][0], pair[0], pair[1][1]))
    return [index for index in range(len(items)) if best_mask & (1 << index)]


def build_plan(shift, jobs, revision=0, generated_at=None):
    starts_at = _at(shift["starts_at"])
    generated_at = generated_at or datetime.now().isoformat(timespec="seconds")
    stations = [dict(station) for station in shift["stations"]]
    known = {station["station_id"] for station in stations}

    locked, candidates, incomplete, revoked, unassigned = [], [], [], [], []
    for job in jobs:
        status = job.get("status") or job_status(job, shift["stations"])
        if status == "REVOKED":
            revoked.append(job)
        elif job.get("started"):
            if job.get("station_id") not in known:
                raise DomainIssue("STATION_NOT_FOUND", 409,
                                  detail="已开工返工 %s 的工位不在本班次" % job.get("job_id"))
            locked.append(job)
        elif status == "INCOMPLETE":
            incomplete.append(job)
        elif status == "UNASSIGNED":
            unassigned.append(job)
        else:
            candidates.append(job)

    plan_stations = []
    deferred = []
    deferred_ids = set()
    issues = []

    def defer(job, station_id, reason, detail):
        if job["job_id"] in deferred_ids:
            return
        deferred_ids.add(job["job_id"])
        deferred.append({"job_id": job["job_id"], "station_id": station_id,
                         "reason": reason, "detail": detail})
    scheduled_count = 0
    total_salvaged = 0
    urgent_total = 0
    urgent_on_time = 0

    for station in stations:
        station_id = station["station_id"]
        capacity = int(station["minutes"])
        items = []
        station_locked = sorted(
            (job for job in locked if job.get("station_id") == station_id),
            key=lambda job: (job.get("started_at") or "", job["job_id"]))
        locked_minutes = sum(int(job["expected_minutes"]) for job in station_locked)
        if locked_minutes > capacity:
            # 已开工单不可挪，只能把超占如实暴露出来并阻断签发。
            issues.append({"job_id": None, "station_id": station_id, "code": "CAPACITY_OVERFLOW",
                           "detail": "工位 %s 已开工返工合计 %d 分钟，超过可用 %d 分钟"
                                     % (station_id, locked_minutes, capacity)})

        # 已开工段按真实开工时刻落位：不为了排得下而前移或后移，重叠只报人工核对。
        cursor = 0
        for job in station_locked:
            minutes = int(job["expected_minutes"])
            offset = int((_at(job["started_at"]) - starts_at).total_seconds() // 60)
            start_minute = offset
            if offset < 0:
                start_minute = 0
                issues.append({"job_id": job["job_id"], "code": "STARTED_BEFORE_SHIFT",
                               "detail": "已开工时刻早于本班开始，占用按班次起点计"})
            if start_minute < cursor:
                issues.append({"job_id": job["job_id"], "code": "STARTED_OVERLAP",
                               "detail": "已开工返工与其它已开工返工时间重叠，需人工核对"})
            item = _item(job, start_minute, start_minute + minutes, starts_at, ROLE_STARTED,
                         _due(job.get("due_at")))
            items.append(item)
            cursor = max(cursor, start_minute + minutes)
            scheduled_count += 1
            total_salvaged += int(job.get("salvage_qty") or 0)
            if item["end_minute"] > capacity:
                issues.append({"job_id": job["job_id"], "code": "STARTED_OVER_CAPACITY",
                               "detail": "已开工返工按实际开工时刻会超出本班工时，需人工处理"})
            if not item["on_time"]:
                issues.append({"job_id": job["job_id"], "code": "STARTED_LATE",
                               "detail": "已开工返工预计完成晚于交期，保留占用"})
        started_minutes = cursor
        room = capacity - started_minutes

        pool = sorted(
            (job for job in candidates if job.get("station_id") == station_id),
            key=lambda job: (job.get("due_at") or "", job["job_id"]))
        urgent_pool = [job for job in pool if is_urgent(job, shift)]
        urgent_total += len(urgent_pool)

        # 1) 急单整批试排：能整批在交期前完成才占用，否则让出优先位。
        committed, missed_urgent, used = [], set(), 0
        for job in urgent_pool:
            minutes = int(job["expected_minutes"])
            end = started_minutes + used + minutes
            if end - started_minutes <= room and _plus(starts_at, end) <= _due(job["due_at"]):
                committed.append(job)
                used += minutes
            else:
                missed_urgent.add(job["job_id"])
        committed_ids = {job["job_id"] for job in committed}

        # 2) 余下工时按 0/1 背包尽量多救料（整单排入，不拆单）。
        rest = [job for job in pool if job["job_id"] not in committed_ids]
        chosen = knapsack(rest, room - used)
        chosen_ids = {rest[index]["job_id"] for index in chosen}
        selected = sorted((job for job in rest if job["job_id"] in chosen_ids),
                          key=lambda job: (job["due_at"], job["job_id"]))
        committed.sort(key=lambda job: (job["due_at"], job["job_id"]))

        cursor = started_minutes
        for job, role in [(job, ROLE_URGENT) for job in committed] + \
                         [(job, ROLE_SALVAGE) for job in selected]:
            minutes = int(job["expected_minutes"])
            item = _item(job, cursor, cursor + minutes, starts_at, role, _due(job.get("due_at")))
            items.append(item)
            cursor += minutes
            scheduled_count += 1
            total_salvaged += int(job.get("salvage_qty") or 0)
            if role == ROLE_URGENT and item["on_time"]:
                urgent_on_time += 1

        remaining = capacity - cursor
        for job in pool:
            if job["job_id"] in committed_ids or job["job_id"] in chosen_ids:
                continue
            if job["job_id"] in missed_urgent:
                reason, detail = "DEADLINE", "急单整批无法在交期前完成，未占用本班工时"
            elif int(job["expected_minutes"]) > remaining:
                reason, detail = "CAPACITY", "剩余工时 %d 分钟，未排入" % remaining
            else:
                reason, detail = "NOT_SELECTED", "为多救料让位给其它工单"
            defer(job, station_id, reason, detail)

        plan_stations.append({
            "station_id": station_id,
            "capacity_minutes": capacity,
            "started_minutes": started_minutes,
            "used_minutes": cursor,
            "idle_minutes": remaining,
            "over_capacity": cursor > capacity,
            "salvaged": sum(item["salvage_qty"] for item in items),
            "items": items,
        })

    for job in incomplete:
        defer(job, job.get("station_id"), "INCOMPLETE", "待补录预计工时、交期或可救件数")

    for job in unassigned:
        defer(job, job.get("station_id"), "NO_STATION", "未指定本班次工位，暂不占用工时")

    for job in revoked:
        defer(job, job.get("station_id"), "RECEIPT_MISMATCH", "实收对账有差异，未开工返工已撤销占用")

    total_capacity = sum(station["capacity_minutes"] for station in plan_stations)
    for job in locked:
        if job.get("received_qty") is not None and job["received_qty"] != job.get("report_qty"):
            issues.append({"job_id": job["job_id"], "code": "STARTED_MISMATCH",
                           "detail": "已开工返工实收与报工不一致，保留占用，需人工核对"})

    return {
        "shift_id": shift["shift_id"],
        "revision": int(revision),
        "generated_at": generated_at,
        "starts_at": shift["starts_at"],
        "ends_at": shift["ends_at"],
        "stations": plan_stations,
        "deferred": deferred,
        "issues": issues,
        "totals": {
            "capacity_minutes": total_capacity,
            "used_minutes": sum(station["used_minutes"] for station in plan_stations),
            "idle_minutes": sum(station["idle_minutes"] for station in plan_stations),
            "jobs_scheduled": scheduled_count,
            "jobs_deferred": len(deferred),
            "jobs_revoked": len(revoked),
            "urgent_total": urgent_total,
            "urgent_on_time": urgent_on_time,
            "salvaged": total_salvaged,
            "over_capacity": any(station["over_capacity"] for station in plan_stations),
        },
    }


def _item(job, start_minute, end_minute, starts_at, role, due):
    return {
        "job_id": job["job_id"],
        "report_run_id": job.get("report_run_id"),
        "station_id": job.get("station_id"),
        "minutes": int(job["expected_minutes"]),
        "salvage_qty": int(job.get("salvage_qty") or 0),
        "due_at": job.get("due_at"),
        "start_minute": start_minute,
        "end_minute": end_minute,
        "start_at": _plus(starts_at, start_minute).isoformat(timespec="seconds"),
        "end_at": _plus(starts_at, end_minute).isoformat(timespec="seconds"),
        "role": role,
        "on_time": True if due is None else _plus(starts_at, end_minute) <= due,
    }
