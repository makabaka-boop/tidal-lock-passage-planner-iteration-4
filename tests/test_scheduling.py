"""核心算法测试：手工边界用例 + 随机小规模与逐秒穷举一致。"""
from __future__ import annotations

import random

from app import scheduling


def brute_force(legs, waits, windows_by_gate, search):
    """逐秒穷举可行出发时刻（仅用于小规模对照）。"""
    lo, hi = search
    feasible = []
    for dep in range(lo, hi):
        result = scheduling.forward_trace(
            dep, legs, waits, windows_by_gate
        )
        if isinstance(result, scheduling.Trace):
            feasible.append(dep)
    # 合并相邻整数点为 [lo, hi+1)
    merged: list[list[int]] = []
    for t in feasible:
        if merged and t == merged[-1][1]:
            merged[-1][1] = t + 1
        else:
            merged.append([t, t + 1])
    return merged


def ws(*pairs):
    return [list(p[0] for p in pairs), list(p[1] for p in pairs)]


def test_entry_exactly_at_end_is_closed():
    starts, ends = [10], [20]
    # 恰在 end 到达 -> 关闭，且后面无窗口
    assert scheduling.earliest_entry(20, starts, ends) is None
    # 提前一秒可以入
    assert scheduling.earliest_entry(19, starts, ends) == 19
    # 恰在 start 到达可以入
    assert scheduling.earliest_entry(10, starts, ends) == 10


def test_single_gate_wait_deadline_boundary():
    # 窗口 [10,20)，最大等待 3：到达 7 等待 3 可行；到达 6 等待 4 不可行。
    wb = ws((10, 20))
    iv = scheduling.feasible_departures([], [3], [wb], (0, 30))
    assert iv == [[7, 20]]  # [7,10) 等待 + [10,20) 窗口内，相接合并


def test_arrival_at_end_closed_propagates():
    # 到达 20 恰在 end，不可行；窗口内部只到 20（不含）。
    wb = ws((10, 20))
    iv = scheduling.feasible_departures([], [0], [wb], (0, 30))
    assert iv == [[10, 20]]
    # 到达 20 之后无窗口
    trace = scheduling.forward_trace(20, [], [0], [wb])
    assert isinstance(trace, scheduling.GateFailure)
    assert trace.reason == "NO_OPEN_WINDOW"


def test_two_gates_with_gap_and_wait():
    # G1 [10,20) wait 0；航行 5；G2 [25,30) wait 0。
    # 首闸入闸需恰好 [20,25)（+5 后落入 [25,30)）；但 G1 只能 [10,20)
    # 入闸，故入闸 20 来自首闸窗口不可达 -> 空。
    w1 = ws((10, 20))
    w2 = ws((25, 30))
    iv = scheduling.feasible_departures([5], [0, 0], [w1, w2], (0, 50))
    assert iv == []
    # 允许 G1 窗口内入闸 15..19 -> 到达 G2 20..24，G2 窗口 25，
    # 若 G2 max_wait >= 5 则可行（到达 20 等 5）。
    iv = scheduling.feasible_departures([5], [0, 5], [w1, w2], (0, 50))
    assert iv == [[15, 20]]


def test_touching_windows_share_boundary():
    # [10,20) 与 [20,30) 相接：到达 20 落入第二窗。
    wb = ws((10, 20), (20, 30))
    assert scheduling.earliest_entry(20, wb[0], wb[1]) == 20
    iv = scheduling.feasible_departures([], [0], [wb], (0, 40))
    assert iv == [[10, 30]]


def test_no_solution_returns_empty():
    wb = ws((100, 110))
    iv = scheduling.feasible_departures([], [0], [wb], (0, 50))
    assert iv == []


def test_search_clipping_half_open():
    wb = ws((10, 100))
    iv = scheduling.feasible_departures([], [1000], [wb], (50, 60))
    assert iv == [[50, 60]]


def test_multi_window_union_merges_across_windows():
    # [10,20), [30,40)，max_wait 10：
    # 第一窗：等待 [0,10) + 内部 [10,20) -> [0,20)；
    # 第二窗：等待 [20,30)（前窗 end=20 为界）+ 内部 [30,40) -> [20,40)；
    # 两段在 20 相接，合并为 [0,40)。
    wb = ws((10, 20), (30, 40))
    iv = scheduling.feasible_departures([], [10], [wb], (0, 50))
    assert iv == [[0, 40]]
    # 等待无限大时仍然只是各窗口与等待片段之并：到 40 为止
    merged = scheduling.feasible_departures([], [1000], [wb], (0, 50))
    assert merged == [[0, 40]]


def test_random_small_cases_match_brute_force():
    rng = random.Random(20260921)
    for case in range(300):
        n = rng.randint(1, 4)
        windows_by_gate = []
        for _ in range(n):
            starts, ends = [], []
            t = rng.randint(0, 5)
            count = rng.randint(1, 4)
            for _ in range(count):
                start = t + rng.randint(0, 6)
                end = start + rng.randint(1, 6)
                if starts and start < ends[-1]:
                    start = ends[-1]
                    end = start + rng.randint(1, 6)
                starts.append(start)
                ends.append(end)
                t = end + rng.randint(0, 3)
            windows_by_gate.append((starts, ends))
        legs = [rng.randint(0, 8) for _ in range(n - 1)]
        waits = [rng.randint(0, 8) for _ in range(n)]
        search = (0, 60)
        got = scheduling.feasible_departures(
            legs, waits, windows_by_gate, search
        )
        want = brute_force(legs, waits, windows_by_gate, search)
        assert got == want, (
            f"case {case}: n={n} legs={legs} waits={waits} "
            f"windows={windows_by_gate} got={got} want={want}"
        )


def boolean_segments(old_windows, new_windows, legs, waits, search):
    """逐秒布尔预言机：每个整数秒独立前向模拟并压缩同标记片段。"""
    lo, hi = search
    expected = []
    for t in range(lo, hi):
        old_ok = isinstance(
            scheduling.forward_trace(t, legs, waits, old_windows),
            scheduling.Trace,
        )
        new_ok = isinstance(
            scheduling.forward_trace(t, legs, waits, new_windows),
            scheduling.Trace,
        )
        if old_ok and new_ok:
            mark = scheduling.BOTH_FEASIBLE
        elif old_ok:
            mark = scheduling.OLD_ONLY
        elif new_ok:
            mark = scheduling.NEW_ONLY
        else:
            continue
        if expected and expected[-1][1] == t and expected[-1][2] == mark:
            expected[-1][1] = t + 1
        else:
            expected.append([t, t + 1, mark])
    return expected


def boolean_intervals(windows_by_gate, legs, waits, search):
    """逐秒布尔预言机产出的最大可行出发区间。"""
    lo, hi = search
    intervals = []
    for t in range(lo, hi):
        if isinstance(
            scheduling.forward_trace(t, legs, waits, windows_by_gate),
            scheduling.Trace,
        ):
            if intervals and intervals[-1][1] == t:
                intervals[-1][1] = t + 1
            else:
                intervals.append([t, t + 1])
    return intervals


def test_compare_adjacent_windows_and_half_open_boundaries():
    # 旧版 [2,4) 与 [4,6) 相接；新版只保留前半段。
    old_windows = [ws((2, 4), (4, 6))]
    new_windows = [ws((2, 4))]
    legs, waits, search = [], [0], (0, 8)
    old_intervals = scheduling.feasible_departures(
        legs, waits, old_windows, search
    )
    new_intervals = scheduling.feasible_departures(
        legs, waits, new_windows, search
    )
    assert old_intervals == [[2, 6]]
    assert new_intervals == [[2, 4]]
    got = scheduling.compare_departure_intervals(old_intervals, new_intervals)
    assert got == [
        [2, 4, scheduling.BOTH_FEASIBLE],
        [4, 6, scheduling.OLD_ONLY],
    ]


def test_compare_empty_sets_omit_infeasible_gaps():
    got = scheduling.compare_departure_intervals([], [])
    assert got == []
    got = scheduling.compare_departure_intervals([[2, 3], [7, 8]], [])
    assert got == [
        [2, 3, scheduling.OLD_ONLY],
        [7, 8, scheduling.OLD_ONLY],
    ]


def test_compare_does_not_merge_across_infeasible_gap_or_different_labels():
    old = [[0, 3], [3, 4], [8, 10]]
    new = [[3, 4]]
    got = scheduling.compare_departure_intervals(old, new)
    assert got == [
        [0, 3, scheduling.OLD_ONLY],
        [3, 4, scheduling.BOTH_FEASIBLE],
        [8, 10, scheduling.OLD_ONLY],
    ]


def test_compare_swapping_calendars_swaps_old_and_new_labels():
    old = [[1, 5], [8, 10]]
    new = [[4, 6], [9, 12]]
    forward = scheduling.compare_departure_intervals(old, new)
    swapped = scheduling.compare_departure_intervals(new, old)
    swapped_labels = {
        scheduling.OLD_ONLY: scheduling.NEW_ONLY,
        scheduling.NEW_ONLY: scheduling.OLD_ONLY,
        scheduling.BOTH_FEASIBLE: scheduling.BOTH_FEASIBLE,
    }
    assert forward == [
        [1, 4, scheduling.OLD_ONLY],
        [4, 5, scheduling.BOTH_FEASIBLE],
        [5, 6, scheduling.NEW_ONLY],
        [8, 9, scheduling.OLD_ONLY],
        [9, 10, scheduling.BOTH_FEASIBLE],
        [10, 12, scheduling.NEW_ONLY],
    ]
    assert swapped == [[a, b, swapped_labels[label]] for a, b, label in forward]


def test_random_compare_matches_second_by_second_oracle():
    rng = random.Random(20260930)
    for case in range(200):
        n = rng.randint(1, 3)
        old_calendar = []
        new_calendar = []
        for _ in range(n):
            variants = []
            for _ in range(2):
                starts, ends = [], []
                t = rng.randint(0, 3)
                for _ in range(rng.randint(1, 4)):
                    start = t + rng.randint(0, 5)
                    end = start + rng.randint(1, 4)
                    if starts and start < ends[-1]:
                        start = ends[-1]
                        end = start + rng.randint(1, 4)
                    starts.append(start)
                    ends.append(end)
                    t = end + rng.randint(0, 3)
                variants.append((starts, ends))
            old_calendar.append(variants[0])
            new_calendar.append(variants[1])
        legs = [rng.randint(0, 6) for _ in range(n - 1)]
        waits = [rng.randint(0, 6) for _ in range(n)]
        search = (0, 35)
        old_intervals = scheduling.feasible_departures(
            legs, waits, old_calendar, search
        )
        new_intervals = scheduling.feasible_departures(
            legs, waits, new_calendar, search
        )
        assert old_intervals == boolean_intervals(
            old_calendar, legs, waits, search
        )
        assert new_intervals == boolean_intervals(
            new_calendar, legs, waits, search
        )
        got = scheduling.compare_departure_intervals(
            old_intervals, new_intervals
        )
        want = boolean_segments(
            old_calendar, new_calendar, legs, waits, search
        )
        assert got == want, (
            f"case {case}: old={old_calendar} new={new_calendar} "
            f"legs={legs} waits={waits}"
        )


def test_feasible_departure_trace_succeeds_and_witness():
    w1 = ws((10, 20))
    w2 = ws((25, 35))
    iv = scheduling.feasible_departures([5], [3, 3], [w1, w2], (0, 50))
    # 至少应有可行区间；区间内逐点前向模拟都应成功，区间外失败
    assert iv
    lo, hi = iv[0]
    good = scheduling.forward_trace(lo, [5], [3, 3], [w1, w2])
    assert isinstance(good, scheduling.Trace)
    assert good.entries[0] >= good.arrivals[0]
    # 恰在右端点（区间外）必须失败
    bad = scheduling.forward_trace(hi, [5], [3, 3], [w1, w2])
    assert isinstance(bad, scheduling.GateFailure)
