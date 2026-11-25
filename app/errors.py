from flask import jsonify


MESSAGES = {
    "EMPTY_FILE": "文件为空",
    "BLANK_FILE": "文件只有空白",
    "HEADER_ONLY": "文件只有表头",
    "HEADER_MISMATCH": "表头与本项目字段不一致",
    "UNCLOSED_QUOTE": "引号未闭合，文件已拒收",
    "TOO_MANY_ROWS": "数据行超过上限",
    "FILE_TOO_LARGE": "文件超过大小上限",
    "KEY_MISSING": "主键为空",
    "CONFLICT_DUPLICATE": "同一键存在冲突内容",
    "SAMPLE_UNKNOWN": "样例不存在",
    "STAGE_UNKNOWN": "阶段不存在",
    "RUN_NOT_FOUND": "批次不存在",
    "STALE_REVIEW": "批次已被更新，请刷新后复核",
    "GATE_BLOCKED": "存在阻断项，不能签发",
    "NOT_APPROVED": "批次尚未签发",
    "BAD_REQUEST": "请求内容不完整",
    "SHIFT_NOT_FOUND": "班次不存在",
    "JOB_NOT_FOUND": "返工单不存在",
    "JOB_EXISTS": "返工单已存在",
    "STATION_NOT_FOUND": "工位不在本班次",
    "REPORT_NOT_APPROVED": "报工尚未签发，不能排返工",
    "STARTED_JOB_INCOMPLETE": "已开工返工单缺少工时或工位",
    "STARTED_LOCKED": "已开工的返工不能挪动",
    "STALE_SCHEDULE": "班次排程已被更新，请刷新后重排",
    "SALVAGE_OVER_REPORT": "可救件数不能超过报工数",
    "SALVAGE_OVER_RECEIPT": "可救件数不能超过实收数",
    "BAD_QUANTITY": "数量不合法",
    "BAD_MINUTES": "预计工时必须为正整数",
    "BAD_MOMENT": "日期时间格式不正确",
}


class DomainIssue(Exception):
    def __init__(self, code: str, status: int = 400, detail: str | None = None):
        self.code = code
        self.status = status
        self.detail = detail
        super().__init__(code)


def error_response(exc: DomainIssue):
    return jsonify(error=exc.detail or MESSAGES.get(exc.code, "请求无法处理"), code=exc.code), exc.status
