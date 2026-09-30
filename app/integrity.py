"""持久化记录的完整性校验。

服务升级或从备份恢复后，调度员会继续使用既有闸门日历与方案。持久化记录
可能不满足当前域约束：同一日历内 gate_id 重复、position 重复，窗口字段
不是合法 JSON、不是二元整数区间、越界、倒置或相互重叠；方案的航程定义
或见证也可能损坏。

本模块在*读取既有记录*时统一做整版校验，任何损坏都抛出 :class:`DataAnomaly`
（HTTP 422，稳定且可区分的错误码），而不是让上层静默挑选记录或因内部解析
异常产生 500。校验全程只读：不新增方案，也不改写任何日历、方案与见证。
新发布日历的整版原子拒绝由 ``schemas`` 在写入前完成，与本模块无关。
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .database import CalendarRow, GateRow, PlanRow
from .scheduling import earliest_entry
from .schemas import MAX_TIME

# ---- 数据异常类型（稳定错误码，客户端可据此区分处理） --------------------

# 日历：一行闸门都没有（典型的部分恢复/缺记录）。
CALENDAR_NO_GATES = "CALENDAR_NO_GATES"
# 日历：同一 position 出现多扇闸门，详情顺序无法稳定确定。
CALENDAR_DUPLICATE_POSITION = "CALENDAR_DUPLICATE_POSITION"
# 日历：同一 gate_id 出现多行，读取/探测/采纳不得静默二选一。
CALENDAR_DUPLICATE_GATE_ID = "CALENDAR_DUPLICATE_GATE_ID"
# 日历：某闸窗口字段不是合法 JSON 文本。
CALENDAR_GATE_WINDOWS_NOT_JSON = "CALENDAR_GATE_WINDOWS_NOT_JSON"
# 日历：窗口解析后不是非空二元整数区间数组。
CALENDAR_WINDOWS_MALFORMED = "CALENDAR_WINDOWS_MALFORMED"
# 日历：窗口端点超出 [0, 10^12]。
CALENDAR_WINDOW_OUT_OF_DOMAIN = "CALENDAR_WINDOW_OUT_OF_DOMAIN"
# 日历：窗口 start >= end（左闭右开区间倒置）。
CALENDAR_WINDOW_INVERTED = "CALENDAR_WINDOW_INVERTED"
# 日历：窗口未按 start 升序或相互重叠。
CALENDAR_WINDOWS_OVERLAP = "CALENDAR_WINDOWS_OVERLAP"

# 方案：航程定义快照不是合法 JSON。
PLAN_PAYLOAD_NOT_JSON = "PLAN_PAYLOAD_NOT_JSON"
# 方案：航程定义结构/取值不满足域约束（键缺失、长度不符、值非法等）。
PLAN_DEFINITION_MALFORMED = "PLAN_DEFINITION_MALFORMED"
# 方案：见证字段不是合法 JSON。
PLAN_WITNESSES_NOT_JSON = "PLAN_WITNESSES_NOT_JSON"
# 方案：见证结构损坏（非数组、长度不符、元素缺键或类型错误）。
PLAN_WITNESSES_SHAPE_MALFORMED = "PLAN_WITNESSES_SHAPE_MALFORMED"
# 方案：见证与航程定义不一致（闸门次序/到达/入闸/等待对不上）。
PLAN_WITNESS_MISMATCH = "PLAN_WITNESS_MISMATCH"
# 方案：见证形状与等待算术自洽，但语义不可信——
# 到达递推不符、入闸不是原日历最早开放时刻，或等待超出上限。
PLAN_WITNESS_SEMANTIC_MISMATCH = "PLAN_WITNESS_SEMANTIC_MISMATCH"
# 方案：其引用的原日历在库中不存在（典型的部分备份恢复）。
PLAN_CALENDAR_MISSING = "PLAN_CALENDAR_MISSING"


class DataAnomaly(HTTPException):
    """持久化记录不满足当前域约束：稳定的 422 数据异常响应。

    错误体形如::

        {"error": "DATA_ANOMALY", "type": "<稳定错误码>",
         "message": "<可读说明>", ...定位上下文}
    """

    def __init__(self, anomaly_type: str, message: str, **context: object) -> None:
        detail: dict[str, object] = {
            "error": "DATA_ANOMALY",
            "type": anomaly_type,
            "message": message,
        }
        detail.update(context)
        super().__init__(status_code=422, detail=detail)


@dataclass(frozen=True)
class StoredGate:
    """通过完整性校验的既有闸门记录。"""

    position: int
    gate_id: str
    windows: list[list[int]]


def _is_int(value: object) -> bool:
    """持久化里的合法整数秒：bool 不算整数。"""
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_windows(row: GateRow) -> list[list[int]]:
    try:
        windows = json.loads(row.windows)
    except (ValueError, TypeError):
        raise DataAnomaly(
            CALENDAR_GATE_WINDOWS_NOT_JSON,
            f"闸门 {row.gate_id!r} 的窗口字段不是合法 JSON",
            gate_id=row.gate_id,
            position=row.position,
        )

    # 热路径（大日历可达数十万窗口）：用 type() is int 精确判别，
    # bool/float/str 一律拒绝，与 _is_int 语义等价但免去嵌套生成器开销。
    if type(windows) is not list or not windows:
        raise DataAnomaly(
            CALENDAR_WINDOWS_MALFORMED,
            f"闸门 {row.gate_id!r} 的窗口必须是非空数组",
            gate_id=row.gate_id,
            position=row.position,
        )

    prev_end: int | None = None
    for index, pair in enumerate(windows):
        if type(pair) is not list or len(pair) != 2:
            raise DataAnomaly(
                CALENDAR_WINDOWS_MALFORMED,
                f"闸门 {row.gate_id!r} 的第 {index} 个窗口不是二元整数区间",
                gate_id=row.gate_id,
                position=row.position,
                window_index=index,
            )
        start, end = pair
        if type(start) is not int or type(end) is not int:
            raise DataAnomaly(
                CALENDAR_WINDOWS_MALFORMED,
                f"闸门 {row.gate_id!r} 的第 {index} 个窗口不是二元整数区间",
                gate_id=row.gate_id,
                position=row.position,
                window_index=index,
            )
        if start < 0 or end > MAX_TIME:
            raise DataAnomaly(
                CALENDAR_WINDOW_OUT_OF_DOMAIN,
                f"闸门 {row.gate_id!r} 的第 {index} 个窗口端点越界",
                gate_id=row.gate_id,
                position=row.position,
                window_index=index,
                start=start,
                end=end,
            )
        if start >= end:
            raise DataAnomaly(
                CALENDAR_WINDOW_INVERTED,
                f"闸门 {row.gate_id!r} 的第 {index} 个窗口必须满足 start < end",
                gate_id=row.gate_id,
                position=row.position,
                window_index=index,
                start=start,
                end=end,
            )
        if prev_end is not None and start < prev_end:
            raise DataAnomaly(
                CALENDAR_WINDOWS_OVERLAP,
                f"闸门 {row.gate_id!r} 的第 {index} 个窗口与前序窗口重叠",
                gate_id=row.gate_id,
                position=row.position,
                window_index=index,
                start=start,
                previous_end=prev_end,
            )
        prev_end = end
    # 已逐窗校验，直接复用 json.loads 产出的整数列表（不可变语义下安全）。
    return windows


def load_calendar(db: Session, calendar_id: str) -> list[StoredGate]:
    """读取并整版校验既有日历，返回按 position 升序的闸门。

    日历不存在抛 404；任何重复身份、顺序冲突或窗口损坏抛 422 数据异常。
    同一日历绝不会只返回部分结果或静默挑选其中一行。
    """
    cal = db.get(CalendarRow, calendar_id)
    if cal is None:
        raise HTTPException(status_code=404, detail="日历不存在")

    # (position, 主键) 双关键字排序：即使 position 重复，检测顺序仍确定。
    rows = db.scalars(
        select(GateRow)
        .where(GateRow.calendar_id == calendar_id)
        .order_by(GateRow.position, GateRow.id)
    ).all()
    if not rows:
        raise DataAnomaly(
            CALENDAR_NO_GATES,
            f"日历 {calendar_id!r} 没有任何闸门记录",
            calendar_id=calendar_id,
        )

    positions: dict[int, str] = {}
    for row in rows:
        if row.position in positions:
            raise DataAnomaly(
                CALENDAR_DUPLICATE_POSITION,
                f"日历中 position={row.position} 被多扇闸门占用",
                calendar_id=calendar_id,
                position=row.position,
                gate_ids=[positions[row.position], row.gate_id],
            )
        positions[row.position] = row.gate_id

    gate_positions: dict[str, list[int]] = {}
    for row in rows:
        gate_positions.setdefault(row.gate_id, []).append(row.position)
    for row in rows:
        where = gate_positions[row.gate_id]
        if len(where) > 1:
            raise DataAnomaly(
                CALENDAR_DUPLICATE_GATE_ID,
                f"闸门 {row.gate_id!r} 在日历中重复出现",
                calendar_id=calendar_id,
                gate_id=row.gate_id,
                positions=where,
            )

    return [
        StoredGate(
            position=row.position,
            gate_id=row.gate_id,
            windows=_validate_windows(row),
        )
        for row in rows
    ]


def _parse_json(raw: str, not_json_type: str, *, plan_id: str) -> object:
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        raise DataAnomaly(
            not_json_type,
            "持久化字段不是合法 JSON",
            plan_id=plan_id,
        )


def _definition_error(plan_id: str, field: str, message: str) -> DataAnomaly:
    return DataAnomaly(
        PLAN_DEFINITION_MALFORMED,
        message,
        plan_id=plan_id,
        field=field,
    )


def _validate_definition(plan: PlanRow, definition: object) -> dict:
    pid = plan.id
    if not isinstance(definition, dict):
        raise _definition_error(pid, "$", "航程定义必须是 JSON 对象")

    gates = definition.get("gates")
    legs = definition.get("legs")
    waits = definition.get("max_waits")
    if not isinstance(gates, list) or not gates:
        raise _definition_error(pid, "gates", "gates 必须是非空数组")
    if not all(isinstance(g, str) and g for g in gates):
        raise _definition_error(pid, "gates", "闸门 ID 必须是非空字符串")
    if len(set(gates)) != len(gates):
        raise _definition_error(pid, "gates", "航程闸门必须互异")

    n = len(gates)
    if not isinstance(legs, list) or len(legs) != n - 1:
        raise _definition_error(pid, "legs", "legs 长度必须为闸门数 - 1")
    if not isinstance(waits, list) or len(waits) != n:
        raise _definition_error(pid, "max_waits", "max_waits 长度必须等于闸门数")
    if not all(_is_int(v) and 0 <= v <= MAX_TIME for v in legs):
        raise _definition_error(pid, "legs", "航行时长必须是 [0, 10^12] 内的整数")
    if not all(_is_int(v) and 0 <= v <= MAX_TIME for v in waits):
        raise _definition_error(
            pid, "max_waits", "最大等待时长必须是 [0, 10^12] 内的整数"
        )
    if not _is_int(plan.departure) or not (0 <= plan.departure <= MAX_TIME):
        raise _definition_error(pid, "departure", "出发时刻必须在 [0, 10^12] 内")
    return definition


def _validate_witnesses(plan_id: str, definition: dict, witnesses: object) -> list:
    n = len(definition["gates"])
    if not isinstance(witnesses, list):
        raise DataAnomaly(
            PLAN_WITNESSES_SHAPE_MALFORMED,
            "见证必须是 JSON 数组",
            plan_id=plan_id,
        )
    if len(witnesses) != n:
        raise DataAnomaly(
            PLAN_WITNESSES_SHAPE_MALFORMED,
            "见证数量必须与航程闸门数一致",
            plan_id=plan_id,
            expected=n,
            actual=len(witnesses),
        )

    gate_ids: list[str] = definition["gates"]
    for i, item in enumerate(witnesses):
        if not isinstance(item, dict):
            raise DataAnomaly(
                PLAN_WITNESSES_SHAPE_MALFORMED,
                f"第 {i} 条见证不是 JSON 对象",
                plan_id=plan_id,
                witness_index=i,
            )
        if set(item) != {"gate_id", "arrival", "entry", "wait"} or not all(
            _is_int(item[k]) for k in ("arrival", "entry", "wait")
        ) or not isinstance(item["gate_id"], str):
            raise DataAnomaly(
                PLAN_WITNESSES_SHAPE_MALFORMED,
                f"第 {i} 条见证字段缺失或类型错误",
                plan_id=plan_id,
                witness_index=i,
            )
        if item["gate_id"] != gate_ids[i]:
            raise DataAnomaly(
                PLAN_WITNESS_MISMATCH,
                f"第 {i} 条见证的闸门与航程定义不一致",
                plan_id=plan_id,
                witness_index=i,
                expected_gate_id=gate_ids[i],
                actual_gate_id=item["gate_id"],
            )
        arrival, entry, wait = item["arrival"], item["entry"], item["wait"]
        if entry < arrival or wait != entry - arrival:
            raise DataAnomaly(
                PLAN_WITNESS_MISMATCH,
                f"第 {i} 条见证的到达/入闸/等待互相对不上",
                plan_id=plan_id,
                witness_index=i,
                arrival=arrival,
                entry=entry,
                wait=wait,
            )
    return witnesses


def _semantic_error(plan_id: str, index: int, message: str, **ctx: object):
    detail = {"plan_id": plan_id, "witness_index": index}
    detail.update(ctx)
    return DataAnomaly(PLAN_WITNESS_SEMANTIC_MISMATCH, message, **detail)


def validate_witness_semantics(
    db: Session,
    plan: PlanRow,
    definition: dict,
    witnesses: list,
) -> None:
    """校验结构自洽的见证是否真的是原日历上的合法采纳快照。

    与探测、采纳、重放遵守同一套半开窗口与"最早可入"规则：
    * 首闸到达 = 方案出发时刻；后闸到达 = 前闸入闸 + 相邻航行时长；
    * 入闸必须是按最早可入原则得到的时刻（落在窗口内立即入闸，
      否则等待下一窗口起点；恰在 end 到达判为关闭）；
    * 等待 = 入闸 − 到达 不得超过该闸最大等待。
    任一不符抛 :data:`PLAN_WITNESS_SEMANTIC_MISMATCH`；原日历缺失抛
    :data:`PLAN_CALENDAR_MISSING`。全程只读，绝不改写见证。
    """
    gate_ids: list[str] = definition["gates"]
    legs: list[int] = definition["legs"]
    waits: list[int] = definition["max_waits"]
    n = len(gate_ids)

    cal_row = db.get(CalendarRow, plan.calendar_id)
    if cal_row is None:
        raise DataAnomaly(
            PLAN_CALENDAR_MISSING,
            "方案引用的原日历不存在，无法核对见证语义",
            plan_id=plan.id,
            calendar_id=plan.calendar_id,
        )
    # 原日历损坏仍按对应日历异常稳定报告（整版校验，不静默挑选）。
    stored = load_calendar(db, plan.calendar_id)
    by_id = {g.gate_id: g.windows for g in stored}

    expected_arrival = plan.departure
    for i in range(n):
        item = witnesses[i]
        gid = gate_ids[i]
        rec_arrival = item["arrival"]
        entry = item["entry"]

        if i > 0:
            # 后闸到达 = 前闸（见证记录的）入闸 + 相邻航行时长。
            expected_arrival = witnesses[i - 1]["entry"] + legs[i - 1]
        if rec_arrival != expected_arrival:
            raise _semantic_error(
                plan.id,
                i,
                "见证到达时刻与航程递推不一致",
                gate_id=gid,
                expected_arrival=expected_arrival,
                actual_arrival=rec_arrival,
            )
        windows = by_id.get(gid)
        if windows is None:
            raise _semantic_error(
                plan.id,
                i,
                "见证闸门不在方案引用的原日历中",
                gate_id=gid,
            )
        starts = [w[0] for w in windows]
        ends = [w[1] for w in windows]
        expected_entry = earliest_entry(rec_arrival, starts, ends)
        if expected_entry is None or entry != expected_entry:
            raise _semantic_error(
                plan.id,
                i,
                "见证入闸时刻不是原日历的最早可入时刻",
                gate_id=gid,
                arrival=rec_arrival,
                expected_entry=expected_entry,
                actual_entry=entry,
            )
        if entry - rec_arrival > waits[i]:
            raise _semantic_error(
                plan.id,
                i,
                "见证等待时长超过该闸最大等待",
                gate_id=gid,
                arrival=rec_arrival,
                entry=entry,
                max_wait=waits[i],
            )


def load_plan(db: Session, plan_id: str) -> tuple[PlanRow, dict, list]:
    """读取并整版校验既有方案，返回 (行, 航程定义, 见证)。

    方案不存在抛 404；航程定义或见证的结构损坏抛 422 数据异常，绝不暴露
    JSON/KeyError 等内部解析异常。见证的*语义*核对（到达递推、最早可入、
    等待上限）由 :func:`load_plan_trusted` 在需要可信快照时追加执行。
    """
    plan = db.get(PlanRow, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="方案不存在")

    definition = _validate_definition(
        plan, _parse_json(plan.payload, PLAN_PAYLOAD_NOT_JSON, plan_id=plan.id)
    )
    witnesses = _parse_json(
        plan.witnesses, PLAN_WITNESSES_NOT_JSON, plan_id=plan.id
    )
    if not isinstance(witnesses, list):
        raise DataAnomaly(
            PLAN_WITNESSES_SHAPE_MALFORMED,
            "见证必须是 JSON 数组",
            plan_id=plan.id,
        )
    _validate_witnesses(plan.id, definition, witnesses)
    return plan, definition, witnesses


def load_plan_trusted(db: Session, plan_id: str) -> tuple[PlanRow, dict, list]:
    """读取既有方案并追加见证语义核对：结构 + 语义均合法才可作为可信快照。"""
    plan, definition, witnesses = load_plan(db, plan_id)
    validate_witness_semantics(db, plan, definition, witnesses)
    return plan, definition, witnesses
