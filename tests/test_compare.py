"""双日历探测比较：差异片段切片 + 小时间域逐秒布尔预言机对照。

独立预言机：对搜索区间内每个整数秒分别用前向模拟判定旧/新版是否
可行，得到两条逐秒布尔标记；期望的可行区间与期望差异片段都由布尔
标记直接推出，与被测的区间传播算法互独立。覆盖相邻窗口、空集、
边界时刻与两份日历顺序互换。
"""
from __future__ import annotations

import json
import random

import pytest

from app import integrity, scheduling
from app.database import CalendarRow, GateRow, PlanRow


# ---- 逐秒布尔预言机 ---------------------------------------------------------


def _flags(legs, waits, windows_by_gate, lo, hi):
    """逐秒布尔标记：每个整数出发时刻是否可行（前向模拟独立判定）。"""
    return [
        isinstance(
            scheduling.forward_trace(t, legs, waits, windows_by_gate),
            scheduling.Trace,
        )
        for t in range(lo, hi)
    ]


def _intervals_from_flags(flags, offset):
    """逐秒布尔标记 -> 合并左闭右开区间。"""
    out = []
    t = 0
    n = len(flags)
    while t < n:
        if not flags[t]:
            t += 1
            continue
        s = t
        while t < n and flags[t]:
            t += 1
        out.append([offset + s, offset + t])
    return out


def _label_of(in_old, in_new):
    if in_old and in_new:
        return "BOTH"
    if in_old:
        return "OLD_ONLY"
    if in_new:
        return "NEW_ONLY"
    return None  # 两版均不可行：不输出


def _segments_from_flags(old_flags, new_flags, offset):
    """两版逐秒布尔标记 -> 期望差异片段（最大连续、同标记相接才合并）。"""
    out = []
    t = 0
    n = len(old_flags)
    while t < n:
        label = _label_of(old_flags[t], new_flags[t])
        if label is None:
            t += 1
            continue
        s = t
        t += 1
        while t < n and _label_of(old_flags[t], new_flags[t]) == label:
            t += 1
        out.append({"start": offset + s, "end": offset + t, "label": label})
    return out


def _wb(windows_list):
    """每闸 [[s, e], ...] 的列表 -> scheduling 用的 (starts, ends) 平行数组。"""
    return [
        ([w[0] for w in windows], [w[1] for w in windows])
        for windows in windows_list
    ]


def _compare(client, old_cid, new_cid, gates, legs, waits, lo, hi):
    resp = client.post(
        "/voyages/probe-compare",
        json={
            "old_calendar_id": old_cid,
            "new_calendar_id": new_cid,
            "gates": gates,
            "legs": legs,
            "max_waits": waits,
            "search_start": lo,
            "search_end": hi,
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


_LABEL_SWAP = {"OLD_ONLY": "NEW_ONLY", "NEW_ONLY": "OLD_ONLY", "BOTH": "BOTH"}


# ---- 差异切片单元用例 -------------------------------------------------------


def test_diff_both_empty():
    assert scheduling.diff_feasible_intervals([], []) == []


def test_diff_one_side_empty():
    assert scheduling.diff_feasible_intervals([[3, 7]], []) == [
        [3, 7, "OLD_ONLY"]
    ]
    assert scheduling.diff_feasible_intervals([], [[3, 7]]) == [
        [3, 7, "NEW_ONLY"]
    ]


def test_diff_identical_is_all_both():
    assert scheduling.diff_feasible_intervals(
        [[1, 4], [6, 9]], [[1, 4], [6, 9]]
    ) == [[1, 4, "BOTH"], [6, 9, "BOTH"]]


def test_diff_touching_segments_different_labels_not_merged():
    # 端点相接但标记不同：不得合并。
    assert scheduling.diff_feasible_intervals([[0, 10]], [[10, 20]]) == [
        [0, 10, "OLD_ONLY"],
        [10, 20, "NEW_ONLY"],
    ]


def test_diff_same_label_separated_by_both_stays_split():
    # 同标记片段被 BOTH 隔断：端点不相接，不合并。
    assert scheduling.diff_feasible_intervals([[0, 30]], [[10, 20]]) == [
        [0, 10, "OLD_ONLY"],
        [10, 20, "BOTH"],
        [20, 30, "OLD_ONLY"],
    ]


def test_diff_gap_between_same_label_stays_split():
    # 两版均不可行的空洞不输出，同标记片段也不得跨洞合并。
    assert scheduling.diff_feasible_intervals([[0, 5], [10, 15]], []) == [
        [0, 5, "OLD_ONLY"],
        [10, 15, "OLD_ONLY"],
    ]


def test_diff_swapped_inputs_exchange_labels():
    old = [[0, 8], [12, 20]]
    new = [[4, 16]]
    forward = scheduling.diff_feasible_intervals(old, new)
    backward = scheduling.diff_feasible_intervals(new, old)
    assert backward == [
        [lo, hi, _LABEL_SWAP[label]] for lo, hi, label in forward
    ]


# ---- API 端到端：相邻窗口、空集、边界时刻 ------------------------------------


def test_compare_end_to_end(client, make_calendar):
    old_cid = make_calendar([[10, 20]], [[25, 35]])
    new_cid = make_calendar([[10, 20]], [[30, 40]])
    body = _compare(client, old_cid, new_cid, ["G1", "G2"], [5], [0, 5], 0, 50)
    # 旧版：G1 入闸 [15,20) 时到达 G2 [20,25)，等 5 内入 [25,35)。
    assert body["old_intervals"] == [[15, 20]]
    # 新版：G2 [30,40) 需到达 [25,40)，但 G1 入闸最晚 19 -> 到达最晚 24 -> 空。
    assert body["new_intervals"] == []
    assert body["segments"] == [
        {"start": 15, "end": 20, "label": "OLD_ONLY"}
    ]


def test_compare_adjacent_windows(client, make_calendar):
    # 相接窗口 [10,20)+[20,30) 合并为 [10,30)；新版只开中段 [15,25)。
    old_cid = make_calendar([[10, 20], [20, 30]])
    new_cid = make_calendar([[15, 25]])
    body = _compare(client, old_cid, new_cid, ["G1"], [], [0], 0, 40)
    assert body["old_intervals"] == [[10, 30]]
    assert body["new_intervals"] == [[15, 25]]
    # 两段 OLD_ONLY 被 BOTH 隔断，不相接、不合并。
    assert body["segments"] == [
        {"start": 10, "end": 15, "label": "OLD_ONLY"},
        {"start": 15, "end": 25, "label": "BOTH"},
        {"start": 25, "end": 30, "label": "OLD_ONLY"},
    ]


def test_compare_empty_sets(client, make_calendar):
    # 两版在搜索区间内都不可行：不输出任何片段。
    old_cid = make_calendar([[100, 110]])
    new_cid = make_calendar([[120, 130]])
    body = _compare(client, old_cid, new_cid, ["G1"], [], [0], 0, 50)
    assert body["old_intervals"] == []
    assert body["new_intervals"] == []
    assert body["segments"] == []
    # 一侧为空：另一侧整段标为单版可行。
    new_cid2 = make_calendar([[10, 20]])
    body = _compare(client, old_cid, new_cid2, ["G1"], [], [0], 0, 50)
    assert body["old_intervals"] == []
    assert body["new_intervals"] == [[10, 20]]
    assert body["segments"] == [
        {"start": 10, "end": 20, "label": "NEW_ONLY"}
    ]


def test_compare_boundary_moments(client, make_calendar):
    # 恰在窗口 end：旧版 [10,20) 在 20 关闭，新版 [20,30) 在 20 开放；
    # 相接但标记不同，不得合并。
    old_cid = make_calendar([[10, 20]])
    new_cid = make_calendar([[20, 30]])
    body = _compare(client, old_cid, new_cid, ["G1"], [], [0], 0, 40)
    assert body["segments"] == [
        {"start": 10, "end": 20, "label": "OLD_ONLY"},
        {"start": 20, "end": 30, "label": "NEW_ONLY"},
    ]
    # 搜索区间端点裁剪：片段被裁到搜索区间边界（左闭右开）。
    body = _compare(client, old_cid, new_cid, ["G1"], [], [0], 12, 28)
    assert body["old_intervals"] == [[12, 20]]
    assert body["new_intervals"] == [[20, 28]]
    assert body["segments"] == [
        {"start": 12, "end": 20, "label": "OLD_ONLY"},
        {"start": 20, "end": 28, "label": "NEW_ONLY"},
    ]
    # 等待上限边界：wait 3 -> 出发 7 可行（等到 10 恰为 3），出发 6 不可行。
    old_wait = make_calendar([[10, 20]])
    new_wait = make_calendar([[13, 20]])
    body = _compare(client, old_wait, new_wait, ["G1"], [], [3], 0, 30)
    assert body["old_intervals"] == [[7, 20]]
    assert body["new_intervals"] == [[10, 20]]
    assert body["segments"] == [
        {"start": 7, "end": 10, "label": "OLD_ONLY"},
        {"start": 10, "end": 20, "label": "BOTH"},
    ]


def test_compare_swapped_calendar_order(client, make_calendar):
    old_cid = make_calendar([[10, 20]], [[25, 35]])
    new_cid = make_calendar([[12, 22]], [[25, 35]])
    gates, legs, waits = ["G1", "G2"], [5], [2, 4]
    fwd = _compare(client, old_cid, new_cid, gates, legs, waits, 0, 60)
    rev = _compare(client, new_cid, old_cid, gates, legs, waits, 0, 60)
    # 顺序互换：原始区间互换，OLD_ONLY/NEW_ONLY 互换，BOTH 不变。
    assert rev["old_intervals"] == fwd["new_intervals"]
    assert rev["new_intervals"] == fwd["old_intervals"]
    assert rev["segments"] == [
        {"start": s["start"], "end": s["end"], "label": _LABEL_SWAP[s["label"]]}
        for s in fwd["segments"]
    ]


# ---- 随机小规模：逐秒布尔预言机对照 + 顺序互换 --------------------------------


def _random_windows(rng):
    windows = []
    t = rng.randint(0, 4)
    for _ in range(rng.randint(1, 4)):
        start = t + rng.randint(0, 5)
        end = start + rng.randint(1, 5)
        if windows and start < windows[-1][1]:
            start = windows[-1][1]
            end = start + rng.randint(1, 5)
        windows.append([start, end])
        t = end + rng.randint(0, 3)
    return windows


def test_compare_random_matches_per_second_oracle(client, make_calendar):
    rng = random.Random(20261001)
    lo, hi = 0, 60
    for case in range(150):
        n = rng.randint(1, 3)
        old_windows = [_random_windows(rng) for _ in range(n)]
        new_windows = [_random_windows(rng) for _ in range(n)]
        old_cid = make_calendar(*old_windows)
        new_cid = make_calendar(*new_windows)
        gates = [f"G{i + 1}" for i in range(n)]
        legs = [rng.randint(0, 6) for _ in range(n - 1)]
        waits = [rng.randint(0, 6) for _ in range(n)]

        got = _compare(client, old_cid, new_cid, gates, legs, waits, lo, hi)

        old_flags = _flags(legs, waits, _wb(old_windows), lo, hi)
        new_flags = _flags(legs, waits, _wb(new_windows), lo, hi)
        ctx = f"case {case}: legs={legs} waits={waits}"
        assert got["old_intervals"] == _intervals_from_flags(old_flags, lo), ctx
        assert got["new_intervals"] == _intervals_from_flags(new_flags, lo), ctx
        assert got["segments"] == _segments_from_flags(
            old_flags, new_flags, lo
        ), ctx

        # 两份日历顺序互换：区间互换、单版标记互换、BOTH 不变。
        rev = _compare(client, new_cid, old_cid, gates, legs, waits, lo, hi)
        assert rev["old_intervals"] == got["new_intervals"], ctx
        assert rev["new_intervals"] == got["old_intervals"], ctx
        assert rev["segments"] == [
            {
                "start": s["start"],
                "end": s["end"],
                "label": _LABEL_SWAP[s["label"]],
            }
            for s in got["segments"]
        ], ctx


# ---- 损坏数据整次拒绝与只读性 ------------------------------------------------


def _seed_calendar(db, cid, gates):
    """gates: [(position, gate_id, windows_list), ...]，直接预置坏数据。"""
    db.add(CalendarRow(id=cid))
    for position, gid, windows in gates:
        db.add(
            GateRow(
                calendar_id=cid,
                position=position,
                gate_id=gid,
                windows=json.dumps(windows),
            )
        )
    db.commit()


@pytest.fixture()
def db(client):
    """复用 client 的全新内存表，直接拿一个 Session 预置坏数据。"""
    from app.database import SessionLocal

    session = SessionLocal()
    yield session
    session.close()


def test_compare_corrupt_calendar_rejects_wholesale(client, db, make_calendar):
    good = make_calendar([[10, 20]])
    _seed_calendar(db, "overlap", [(0, "G1", [[0, 10], [5, 15]])])
    voyage = {
        "gates": ["G1"],
        "legs": [],
        "max_waits": [0],
        "search_start": 0,
        "search_end": 30,
    }
    plans_before = db.query(PlanRow).count()

    # 旧版损坏、新版损坏、两版都损坏：一律整次 422 数据异常。
    for old_id, new_id in (
        ("overlap", good),
        (good, "overlap"),
        ("overlap", "overlap"),
    ):
        resp = client.post(
            "/voyages/probe-compare",
            json={
                "old_calendar_id": old_id,
                "new_calendar_id": new_id,
                **voyage,
            },
        )
        assert resp.status_code == 422, resp.text
        detail = resp.json()["detail"]
        assert detail["error"] == "DATA_ANOMALY"
        assert detail["type"] == integrity.CALENDAR_WINDOWS_OVERLAP

    # 整次拒绝：不返回一半结果，也不写入任何方案。
    assert db.query(PlanRow).count() == plans_before


def test_compare_missing_calendar_and_unknown_gate(client, make_calendar):
    cid = make_calendar([[10, 20]])
    voyage = {
        "gates": ["G1"],
        "legs": [],
        "max_waits": [0],
        "search_start": 0,
        "search_end": 30,
    }
    for old_id, new_id in (("nope", cid), (cid, "nope")):
        resp = client.post(
            "/voyages/probe-compare",
            json={"old_calendar_id": old_id, "new_calendar_id": new_id, **voyage},
        )
        assert resp.status_code == 404
    # 航程引用日历中不存在的闸门：422（非 DATA_ANOMALY）。
    resp = client.post(
        "/voyages/probe-compare",
        json={
            "old_calendar_id": cid,
            "new_calendar_id": cid,
            **{**voyage, "gates": ["MISSING"]},
        },
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert not (
        isinstance(detail, dict) and detail.get("error") == "DATA_ANOMALY"
    )


def test_compare_bad_voyage_shape(client, make_calendar):
    cid = make_calendar([[0, 10]])
    base = {
        "old_calendar_id": cid,
        "new_calendar_id": cid,
        "gates": ["G1"],
        "legs": [],
        "max_waits": [0],
        "search_start": 0,
        "search_end": 5,
    }
    assert client.post("/voyages/probe-compare", json=base).status_code == 200
    bad = {**base, "gates": ["G1", "G1"], "legs": [1], "max_waits": [0, 0]}
    assert client.post("/voyages/probe-compare", json=bad).status_code == 422
    bad = {**base, "legs": [1]}  # legs 应为 0 个
    assert client.post("/voyages/probe-compare", json=bad).status_code == 422
    bad = {**base, "search_start": 5, "search_end": 5}  # start < end
    assert client.post("/voyages/probe-compare", json=bad).status_code == 422
    bad = {**base, "unknown_field": 1}  # extra="forbid"
    assert client.post("/voyages/probe-compare", json=bad).status_code == 422


def test_compare_is_read_only(client, db, make_calendar):
    old_cid = make_calendar([[10, 20]])
    new_cid = make_calendar([[15, 25]])
    resp = client.post(
        "/voyages/probe-compare",
        json={
            "old_calendar_id": old_cid,
            "new_calendar_id": new_cid,
            "gates": ["G1"],
            "legs": [],
            "max_waits": [0],
            "search_start": 0,
            "search_end": 40,
        },
    )
    assert resp.status_code == 200
    # 只读：不产生方案，两份日历内容均不变。
    assert db.query(PlanRow).count() == 0
    assert client.get(f"/calendars/{old_cid}").json()["gates"] == [
        {"gate_id": "G1", "windows": [[10, 20]]}
    ]
    assert client.get(f"/calendars/{new_cid}").json()["gates"] == [
        {"gate_id": "G1", "windows": [[15, 25]]}
    ]
