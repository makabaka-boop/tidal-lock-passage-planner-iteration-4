"""潮汐闸门调度核心算法。

确定性模型（最早可入原则）：
* 首闸到达时刻 = 出发时刻；
* 第 i 闸到达时刻 = 第 i-1 闸入闸时刻 + 相邻航行时长 legs[i-1]；
* 到达时若落在某窗口 [start, end) 内则立即入闸，否则等待下一个窗口开始；
* 恰在 end 到达视为闸门已关闭；
* 等待时长 = 入闸 - 到达，必须不超过该闸最大等待时长。

可行出发时刻集合通过从末闸向前传播"可行入闸时刻区间集"得到。
逆传区间用两条平行的整数列表（lo/hi）表示，已合并、升序、左闭右开。

每闸的求逆分两类片段：
1. 等待片段 [max(prev_end, s-wait), s)：入闸恒为窗口起点 s，仅当
   s 属于下游可行集时整段可行；
2. 窗口内部 [s, e)：入闸 == 到达，与下游可行集求交。

只有一段窗口的闸门（典型的"全程开放"闸）走 bisect 快速路径：
内部交集只需裁剪首尾、中段整体引用，O(log m) 即可完成；这消除了
"前 199 闸各一段全程窗口、末闸数万稀疏窗口"时每闸 O(m) 的重复物化。
通用多窗口路径仍是一次线性双指针扫描，总体
O(总窗口数 + 区间跨越窗口的换段次数 + 单窗口闸·log m)。
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass

# 时间域为 [0, 10^12]，传播时用一个足够大的右端点作为末闸哨兵。
INF = 10**30

# 双日历比较片段的稳定标记（直接作为响应中的中文标签）。
OLD_ONLY = "仅旧版可行"
NEW_ONLY = "仅新版可行"
BOTH_FEASIBLE = "两版均可行"


@dataclass(frozen=True)
class GateFailure:
    """前向模拟在某闸失败。"""

    gate_index: int
    arrival: int
    reason: str  # NO_OPEN_WINDOW | WAIT_EXCEEDED


@dataclass(frozen=True)
class Trace:
    """前向模拟成功后的逐闸见证数据。"""

    arrivals: list[int]
    entries: list[int]


def earliest_entry(arrival: int, starts: list[int], ends: list[int]) -> int | None:
    """按最早可入原则返回入闸时刻；无未来窗口时返回 None。

    窗口按 start 升序、互不重叠。恰在 end 到达判为关闭（左闭右开）。
    """
    j = bisect_right(starts, arrival) - 1
    if j >= 0 and arrival < ends[j]:
        return arrival  # 已在窗口内，立即入闸
    j += 1  # 等待下一个窗口开始
    if j < len(starts):
        return starts[j]
    return None


def _emit(out_lo: list[int], out_hi: list[int], lo: int, hi: int) -> None:
    """并入一个左闭右开片段；相接或重叠时就地合并到末段。"""
    if lo >= hi:
        return
    if out_hi and lo <= out_hi[-1]:
        if hi > out_hi[-1]:
            out_hi[-1] = hi
    else:
        out_lo.append(lo)
        out_hi.append(hi)


def _preimage_single_window(
    s: int,
    e: int,
    max_wait: int,
    dlo: list[int],
    dhi: list[int],
) -> tuple[list[int], list[int]]:
    """单窗口闸门的求逆：只有一段窗口 [s, e)（j=0，无前置窗口）。

    等待片段为 [s-max_wait, s)（当 s ∈ downstream）；窗口内部为
    downstream 与 [s, e) 的交集。除首尾可能被裁剪外，中段区间整体
    复用，故无需逐区间 Python 循环。
    """
    out_lo: list[int] = []
    out_hi: list[int] = []
    size = len(dlo)

    # 等待片段：恰在 s 入闸要求 s ∈ downstream（左闭右开的成员判定）。
    mi = bisect_right(dlo, s) - 1
    if mi >= 0 and dhi[mi] > s:
        gap_lo = s - max_wait
        if gap_lo < s:
            out_lo.append(gap_lo)
            out_hi.append(s)

    # 与 [s, e) 相交的下游区间下标范围 [i0, i1)。
    i0 = bisect_right(dhi, s)  # 第一个 hi > s
    i1 = bisect_left(dlo, e)  # 第一个 lo >= e
    if i0 < i1:
        _emit(
            out_lo,
            out_hi,
            s if s > dlo[i0] else dlo[i0],
            e if e < dhi[i0] else dhi[i0],
        )
        if i1 - i0 > 2:
            # 中段既不触碰 s 也不触碰 e，原样并入（C 层切片拷贝）。
            out_lo.extend(dlo[i0 + 1 : i1 - 1])
            out_hi.extend(dhi[i0 + 1 : i1 - 1])
        if i1 - i0 > 1:
            _emit(out_lo, out_hi, dlo[i1 - 1], e if e < dhi[i1 - 1] else dhi[i1 - 1])

    return out_lo, out_hi


def _preimage(
    starts: list[int],
    ends: list[int],
    max_wait: int,
    dlo: list[int],
    dhi: list[int],
) -> tuple[list[int], list[int]]:
    """求本闸"可行到达区间"（lo/hi 平行数组）。

    ``dlo/dhi`` 是后续航程要求的入闸时刻集合（已合并、升序、左闭右开）。

    可行到达域 = ⋃_j (等待片段 W_j) ∪ (下游集 ∩ ⋃_j [s_j, e_j))，
    其中 W_j = [max(e_{j-1}, s_j-wait), s_j)（首窗无前置下界），仅当
    s_j ∈ downstream 时有效。窗口内部交集用一次合并式扫描产出：
    窗口指针与下游区间指针都严格单调，窗口切换时把交集指针推进到
    覆盖新窗口起点的区间，绝不回退重置——避免密集区间下的平方退化。
    """
    size = len(dlo)
    if size == 0:
        return [], []
    if len(starts) == 1:
        return _preimage_single_window(starts[0], ends[0], max_wait, dlo, dhi)

    olo: list[int] = []
    ohi: list[int] = []
    apo = olo.append
    hpo = ohi.append

    nwin = len(starts)
    j = 0
    s = starts[0]
    e = ends[0]
    prev_end = -INF  # 首窗之前没有"上一段窗口结束"的下界

    # ---- 等待片段 W_j：s_j ∈ downstream 的窗口才贡献 ----
    m = 0
    # ---- 窗口内部交集 [s,e) ∩ downstream ----
    q = 0

    while j < nwin:
        # 先处理本窗的等待片段（它位于窗口起点左侧，顺序上先于内部交集）。
        bound = s - max_wait
        gap_lo = prev_end if prev_end > bound else bound
        if gap_lo < s:
            while m < size and dhi[m] <= s:
                m += 1
            if m < size and dlo[m] <= s < dhi[m]:
                if ohi and gap_lo <= ohi[-1]:
                    if s > ohi[-1]:
                        ohi[-1] = s
                else:
                    apo(gap_lo)
                    hpo(s)

        # 内部交集：把 q 推进到覆盖/越过窗口起点 s 的下游区间。
        while q < size and dhi[q] <= s:
            q += 1
        while j < nwin and q < size:
            dq_lo = dlo[q]
            dq_hi = dhi[q]
            lo = s if s > dq_lo else dq_lo
            hi = e if e < dq_hi else dq_hi
            if lo < hi:  # 与本窗相交则并入（相接/重叠就地合并）
                if ohi and lo <= ohi[-1]:
                    if hi > ohi[-1]:
                        ohi[-1] = hi
                else:
                    apo(lo)
                    hpo(hi)
            if dq_hi <= e:
                # 该区间在本窗内结束；下一个区间若也在本窗内则继续。
                q += 1
                continue
            # dlo[q] 仍在本窗内但 hi > e：交集随本窗结束；同一下游区间
            # 继续与下一窗相交，q 不前进。
            break
        j += 1
        if j < nwin:
            prev_end = e
            s = starts[j]
            e = ends[j]

    return olo, ohi


def feasible_departures(
    legs: list[int],
    waits: list[int],
    gate_windows: list[tuple[list[int], list[int]]],
    search: tuple[int, int],
) -> list[list[int]]:
    """返回出发搜索区间内全部可行出发时刻的合并区间（左闭右开）。"""
    n = len(waits)
    # 末闸之后：任何入闸时刻都被接受（哨兵区间）。
    dlo: list[int] = [0]
    dhi: list[int] = [INF]

    for i in range(n - 1, -1, -1):
        starts, ends = gate_windows[i]
        if not starts:
            return []  # 该闸永不开放
        dlo, dhi = _preimage(starts, ends, waits[i], dlo, dhi)
        if not dlo:
            return []
        if i > 0:
            # 到达本闸 = 上一闸入闸 + legs[i-1]，平移到上一闸的入闸时刻域。
            d = legs[i - 1]
            if d:
                dlo = [x - d for x in dlo]
                dhi = [x - d for x in dhi]

    # 此时 dlo/dhi 即首闸可行到达域，也就是可行出发时刻域。与搜索区间求交。
    slo, shi = search
    i0 = bisect_right(dhi, slo)  # 第一个 hi > slo
    i1 = bisect_right(dlo, shi)  # 相交要求 lo < shi，排除 lo >= shi
    result: list[list[int]] = []
    for j in range(i0, i1):
        lo = slo if slo > dlo[j] else dlo[j]
        hi = shi if shi < dhi[j] else dhi[j]
        if lo < hi:
            result.append([lo, hi])
    return result


def compare_departure_intervals(
    old_intervals: list[list[int]],
    new_intervals: list[list[int]],
) -> list[list[int | str]]:
    """把两份整数秒可行区间切成按标记区分的最大连续片段。

    输入和输出都使用左闭右开整数区间。同一点的标记由两份区间的逐点
    成员关系决定；两份均不可行的空隙不输出。双指针在任一区间起点或终点
    切换状态，因此相接片段只在端点相等时合并，跨越不可行空隙的片段不会
    合并。
    """
    segments: list[list[int | str]] = []
    i = j = 0
    old_active = new_active = False
    cursor: int | None = None

    def label() -> str | None:
        if old_active and new_active:
            return BOTH_FEASIBLE
        if old_active:
            return OLD_ONLY
        if new_active:
            return NEW_ONLY
        return None

    while (
        i < len(old_intervals)
        or j < len(new_intervals)
        or old_active
        or new_active
    ):
        boundaries: list[int] = []
        if old_active:
            boundaries.append(old_intervals[i][1])
        elif i < len(old_intervals):
            boundaries.append(old_intervals[i][0])
        if new_active:
            boundaries.append(new_intervals[j][1])
        elif j < len(new_intervals):
            boundaries.append(new_intervals[j][0])
        boundary = min(boundaries)

        if cursor is not None and boundary > cursor:
            current = label()
            if current is not None:
                if (
                    segments
                    and segments[-1][2] == current
                    and segments[-1][1] == cursor
                ):
                    segments[-1][1] = boundary
                else:
                    segments.append([cursor, boundary, current])
        cursor = boundary

        if old_active and old_intervals[i][1] == boundary:
            old_active = False
            i += 1
        if new_active and new_intervals[j][1] == boundary:
            new_active = False
            j += 1

        if (
            not old_active
            and i < len(old_intervals)
            and old_intervals[i][0] == boundary
        ):
            old_active = True
        if (
            not new_active
            and j < len(new_intervals)
            and new_intervals[j][0] == boundary
        ):
            new_active = True

    return segments


def forward_trace(
    departure: int,
    legs: list[int],
    waits: list[int],
    gate_windows: list[tuple[list[int], list[int]]],
) -> Trace | GateFailure:
    """按最早可入原则逐闸模拟；返回见证 Trace 或首个失败 GateFailure。"""
    arrivals: list[int] = []
    entries: list[int] = []
    arrival = departure

    for i in range(len(waits)):
        starts, ends = gate_windows[i]
        entry = earliest_entry(arrival, starts, ends) if starts else None
        if entry is None:
            return GateFailure(i, arrival, "NO_OPEN_WINDOW")
        if entry - arrival > waits[i]:
            return GateFailure(i, arrival, "WAIT_EXCEEDED")
        arrivals.append(arrival)
        entries.append(entry)
        if i < len(waits) - 1:
            arrival = entry + legs[i]

    return Trace(arrivals, entries)
