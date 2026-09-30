"""既有持久化记录损坏时的完整性校验：稳定 422、确定结果、绝不改写。

这些用例直接向表中预置「升级/恢复后可能出现」的坏数据（重复闸门、
重复 position、损坏窗口、非法边界、损坏方案），验证读取、探测、采纳
与重放都返回可区分的 DATA_ANOMALY 响应，而不是 500 或静默挑选记录。
"""
from __future__ import annotations

import json

import pytest

from app import integrity
from app.database import CalendarRow, GateRow, PlanRow


def _seed_calendar(db, cid, gates):
    """gates: [(position, gate_id, windows_raw_json|list), ...]"""
    db.add(CalendarRow(id=cid))
    for position, gid, windows in gates:
        raw = windows if isinstance(windows, str) else json.dumps(windows)
        db.add(
            GateRow(
                calendar_id=cid,
                position=position,
                gate_id=gid,
                windows=raw,
            )
        )
    db.commit()


def _seed_plan(db, pid, cid, payload, departure, witnesses):
    db.add(
        PlanRow(
            id=pid,
            calendar_id=cid,
            payload=payload if isinstance(payload, str) else json.dumps(payload),
            departure=departure,
            witnesses=(
                witnesses
                if isinstance(witnesses, str)
                else json.dumps(witnesses)
            ),
        )
    )
    db.commit()


def _definition(gates=("G1",), legs=None, waits=None):
    n = len(gates)
    return {
        "gates": list(gates),
        "legs": list(legs if legs is not None else [0] * (n - 1)),
        "max_waits": list(waits if waits is not None else [100] * n),
    }


def _witnesses(gates=("G1",)):
    return [
        {"gate_id": g, "arrival": 10, "entry": 10, "wait": 0} for g in gates
    ]


@pytest.fixture()
def db(client):
    """复用 client 的全新内存表，直接拿一个 Session 预置坏数据。"""
    from app.database import SessionLocal

    session = SessionLocal()
    yield session
    session.close()


VOYAGE_BODY = {
    "gates": ["G1"],
    "legs": [],
    "max_waits": [100],
    "search_start": 0,
    "search_end": 50,
}
PLAN_BODY = {
    "gates": ["G1"],
    "legs": [],
    "max_waits": [100],
    "departure": 10,
}


def _assert_anomaly(resp, expected_type):
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "DATA_ANOMALY"
    assert detail["type"] == expected_type
    return detail


def test_duplicate_gate_id_all_endpoints_422(client, db):
    _seed_calendar(
        db,
        "dup_gid",
        [
            (0, "G1", [[10, 20]]),
            (1, "G1", [[30, 40]]),
        ],
    )
    # 详情不再同时返回两项
    _assert_anomaly(
        client.get("/calendars/dup_gid"), integrity.CALENDAR_DUPLICATE_GATE_ID
    )
    body = {**VOYAGE_BODY, "calendar_id": "dup_gid"}
    _assert_anomaly(
        client.post("/voyages/probe", json=body),
        integrity.CALENDAR_DUPLICATE_GATE_ID,
    )
    body = {**PLAN_BODY, "calendar_id": "dup_gid"}
    _assert_anomaly(
        client.post("/plans", json=body), integrity.CALENDAR_DUPLICATE_GATE_ID
    )
    # 重放也必须先拒绝损坏日历
    _seed_plan(
        db, "p1", "other", _definition(), 10, _witnesses()
    )
    _assert_anomaly(
        client.post("/plans/p1/replay/dup_gid"),
        integrity.CALENDAR_DUPLICATE_GATE_ID,
    )


def test_duplicate_position_is_stable_anomaly(client, db):
    _seed_calendar(
        db,
        "dup_pos",
        [
            (0, "G1", [[10, 20]]),
            (0, "G2", [[30, 40]]),
        ],
    )
    responses = [
        _assert_anomaly(
            client.get("/calendars/dup_pos"),
            integrity.CALENDAR_DUPLICATE_POSITION,
        )
        for _ in range(3)
    ]
    # 定位上下文稳定
    assert all(r["position"] == 0 for r in responses)
    # 先报顺序冲突，不被重复 gate_id 掩盖
    _seed_calendar(
        db,
        "dup_both",
        [
            (0, "G1", [[10, 20]]),
            (0, "G1", [[30, 40]]),
        ],
    )
    _assert_anomaly(
        client.get("/calendars/dup_both"),
        integrity.CALENDAR_DUPLICATE_POSITION,
    )


def test_windows_not_json(client, db):
    _seed_calendar(db, "bad_json", [(0, "G1", "{not json")])
    _assert_anomaly(
        client.get("/calendars/bad_json"),
        integrity.CALENDAR_GATE_WINDOWS_NOT_JSON,
    )
    body = {**VOYAGE_BODY, "calendar_id": "bad_json"}
    _assert_anomaly(
        client.post("/voyages/probe", json=body),
        integrity.CALENDAR_GATE_WINDOWS_NOT_JSON,
    )


def test_windows_malformed_shapes(client, db):
    cases = {
        "not_array": "123",
        "empty": [],
        "pair_len_1": [[10]],
        "pair_len_3": [[10, 20, 30]],
        "not_ints": [[10, "20"]],
        "bool_pair": [[True, 20]],
    }
    for name, windows in cases.items():
        cid = f"wm_{name}"
        _seed_calendar(db, cid, [(0, "G1", windows)])
        _assert_anomaly(
            client.get(f"/calendars/{cid}"),
            integrity.CALENDAR_WINDOWS_MALFORMED,
        )


def test_window_out_of_domain(client, db):
    for name, windows in (
        ("neg", [[-1, 10]]),
        ("too_big", [[0, 10**12 + 1]]),
    ):
        cid = f"wo_{name}"
        _seed_calendar(db, cid, [(0, "G1", windows)])
        d = _assert_anomaly(
            client.get(f"/calendars/{cid}"),
            integrity.CALENDAR_WINDOW_OUT_OF_DOMAIN,
        )
        assert d["window_index"] == 0


def test_window_inverted_and_overlap(client, db):
    _seed_calendar(db, "inv", [(0, "G1", [[20, 20]])])
    _assert_anomaly(
        client.get("/calendars/inv"), integrity.CALENDAR_WINDOW_INVERTED
    )
    _seed_calendar(db, "inv2", [(0, "G1", [[30, 20]])])
    _assert_anomaly(
        client.get("/calendars/inv2"), integrity.CALENDAR_WINDOW_INVERTED
    )
    # 重叠（含倒置顺序）
    _seed_calendar(db, "ovl", [(0, "G1", [[0, 10], [9, 20]])])
    _assert_anomaly(
        client.get("/calendars/ovl"), integrity.CALENDAR_WINDOWS_OVERLAP
    )
    _seed_calendar(db, "ovl2", [(0, "G1", [[30, 40], [0, 10]])])
    _assert_anomaly(
        client.get("/calendars/ovl2"), integrity.CALENDAR_WINDOWS_OVERLAP
    )
    # 相接合法
    _seed_calendar(db, "touch", [(0, "G1", [[10, 20], [20, 30]])])
    assert client.get("/calendars/touch").status_code == 200


def test_no_gates_calendar(client, db):
    db.add(CalendarRow(id="hollow"))
    db.commit()
    _assert_anomaly(
        client.get("/calendars/hollow"), integrity.CALENDAR_NO_GATES
    )


def test_corrupt_plan_payload_variants(client, db):
    _seed_calendar(db, "good_cal", [(0, "G1", [[0, 100]])])
    # payload 不是 JSON
    _seed_plan(db, "pj", "good_cal", "xxx", 10, _witnesses())
    _assert_anomaly(
        client.get("/plans/pj"), integrity.PLAN_PAYLOAD_NOT_JSON
    )
    # 定义结构损坏
    bad_defs = [
        "[]",  # 不是对象
        {**_definition(), "gates": []},
        {**_definition(), "gates": ["A", "A"]},
        {**_definition(("A", "B")), "legs": []},  # legs 长度不符
        {**_definition(), "max_waits": [1, 2]},
        {**_definition(), "legs": [-1]},
    ]
    for i, payload in enumerate(bad_defs):
        pid = f"pd_{i}"
        _seed_plan(db, pid, "good_cal", payload, 10, _witnesses())
        _assert_anomaly(
            client.get(f"/plans/{pid}"),
            integrity.PLAN_DEFINITION_MALFORMED,
        )


def test_corrupt_witnesses(client, db):
    _seed_calendar(db, "good_cal", [(0, "G1", [[0, 100]])])
    # 见证不是 JSON
    _seed_plan(db, "wj", "good_cal", _definition(), 10, "nope")
    _assert_anomaly(
        client.get("/plans/wj"), integrity.PLAN_WITNESSES_NOT_JSON
    )
    # 见证结构损坏
    shape_bad = ["[]", json.dumps({}), json.dumps([{"gate_id": "G1"}])]
    for i, witnesses in enumerate(shape_bad):
        pid = f"ws_{i}"
        _seed_plan(db, pid, "good_cal", _definition(), 10, witnesses)
        _assert_anomaly(
            client.get(f"/plans/{pid}"),
            integrity.PLAN_WITNESSES_SHAPE_MALFORMED,
        )
    # 见证与定义不一致：闸门次序对不上
    _seed_plan(
        db,
        "wm_1",
        "good_cal",
        _definition(("G1", "G2")),
        10,
        [
            {"gate_id": "G2", "arrival": 10, "entry": 10, "wait": 0},
            {"gate_id": "G1", "arrival": 10, "entry": 10, "wait": 0},
        ],
    )
    _assert_anomaly(
        client.get("/plans/wm_1"), integrity.PLAN_WITNESS_MISMATCH
    )
    # wait != entry - arrival
    _seed_plan(
        db,
        "wm_2",
        "good_cal",
        _definition(),
        10,
        [{"gate_id": "G1", "arrival": 10, "entry": 15, "wait": 99}],
    )
    _assert_anomaly(
        client.get("/plans/wm_2"), integrity.PLAN_WITNESS_MISMATCH
    )


def test_semantically_bad_witnesses_are_anomaly(client, db):
    """形状与每项等待算术都合法、但语义错误的两种历史见证：
    1) 后闸到达 != 前闸入闸 + 航行时长；
    2) 入闸不是原日历的最早开放时刻。
    另含等待超过上限（同样结构自洽）。读取与重放都必须稳定 422，
    且响应体逐字节一致、原表一行不改；合法旧方案继续可读。"""
    _seed_calendar(
        db,
        "sem_cal",
        [(0, "G1", [[10, 20]]), (1, "G2", [[25, 35]])],
    )
    definition = _definition(("G1", "G2"), legs=[5], waits=[5, 5])

    # 1) 到达递推错误：G1 入闸 15 + 航行 5 应为 20，记录成 21；
    #    且每项内部 wait == entry - arrival 仍成立。
    bad_arrival = [
        {"gate_id": "G1", "arrival": 15, "entry": 15, "wait": 0},
        {"gate_id": "G2", "arrival": 21, "entry": 25, "wait": 4},
    ]
    _seed_plan(db, "sem_arr", "sem_cal", definition, 15, bad_arrival)
    d1 = _assert_anomaly(
        client.get("/plans/sem_arr"),
        integrity.PLAN_WITNESS_SEMANTIC_MISMATCH,
    )
    assert d1["witness_index"] == 1
    assert d1["expected_arrival"] == 20
    assert d1["actual_arrival"] == 21

    # 2) 入闸不是最早可入时刻：G2 20 到达、窗口 25 开，
    #    最早可入是 25，记录成 30（仍在窗口内、算术自洽）。
    bad_entry = [
        {"gate_id": "G1", "arrival": 15, "entry": 15, "wait": 0},
        {"gate_id": "G2", "arrival": 20, "entry": 30, "wait": 10},
    ]
    _seed_plan(db, "sem_ent", "sem_cal", definition, 15, bad_entry)
    d2 = _assert_anomaly(
        client.get("/plans/sem_ent"),
        integrity.PLAN_WITNESS_SEMANTIC_MISMATCH,
    )
    assert d2["witness_index"] == 1
    assert d2["expected_entry"] == 25
    assert d2["actual_entry"] == 30

    # 3) 等待超过上限：到达 20、最早入闸 25、等待 5 == 上限合法；
    #    构造到达 19（入闸 25、等待 6 > 5）的结构自洽记录。
    bad_wait = [
        {"gate_id": "G1", "arrival": 14, "entry": 14, "wait": 0},
        {"gate_id": "G2", "arrival": 19, "entry": 25, "wait": 6},
    ]
    _seed_plan(db, "sem_wait", "sem_cal", definition, 14, bad_wait)
    d3 = _assert_anomaly(
        client.get("/plans/sem_wait"),
        integrity.PLAN_WITNESS_SEMANTIC_MISMATCH,
    )
    assert d3["witness_index"] == 1

    # 首闸到达与出发时刻不符：witness_index 0。
    bad_first = [
        {"gate_id": "G1", "arrival": 16, "entry": 16, "wait": 0},
        {"gate_id": "G2", "arrival": 21, "entry": 25, "wait": 4},
    ]
    _seed_plan(db, "sem_first", "sem_cal", definition, 15, bad_first)
    d4 = _assert_anomaly(
        client.get("/plans/sem_first"),
        integrity.PLAN_WITNESS_SEMANTIC_MISMATCH,
    )
    assert d4["witness_index"] == 0

    # 重放同样拒绝不可信来源（即使新日历完全合法）
    _seed_calendar(
        db,
        "sem_new",
        [(0, "G1", [[10, 20]]), (1, "G2", [[25, 35]])],
    )
    rep = client.post("/plans/sem_arr/replay/sem_new")
    assert rep.status_code == 422
    assert rep.json()["detail"]["type"] == integrity.PLAN_WITNESS_SEMANTIC_MISMATCH

    # 响应体重复查询逐字节稳定
    again = client.get("/plans/sem_ent")
    assert again.json()["detail"] == d2

    # 原记录未被改写（见证仍是写入时的坏值）
    row = db.query(PlanRow).filter(PlanRow.id == "sem_arr").one()
    assert json.loads(row.witnesses) == bad_arrival

    # 合法旧方案继续可读、可重放
    good = [
        {"gate_id": "G1", "arrival": 15, "entry": 15, "wait": 0},
        {"gate_id": "G2", "arrival": 20, "entry": 25, "wait": 5},
    ]
    _seed_plan(db, "sem_good", "sem_cal", definition, 15, good)
    assert client.get("/plans/sem_good").status_code == 200
    assert (
        client.post("/plans/sem_good/replay/sem_new").json()["status"]
        == "STILL_VALID"
    )


def test_plan_origin_calendar_missing_is_anomaly(client, db):
    """原日历整版缺失（部分恢复）：方案结构与见证形状合法也无法核对语义，
    返回稳定的 PLAN_CALENDAR_MISSING，而不是 500 或当成有效快照。"""
    definition = _definition(("G1",), legs=[], waits=[100])
    witnesses = [{"gate_id": "G1", "arrival": 10, "entry": 10, "wait": 0}]
    _seed_plan(db, "orphan", "ghost_cal", definition, 10, witnesses)
    d = _assert_anomaly(
        client.get("/plans/orphan"), integrity.PLAN_CALENDAR_MISSING
    )
    assert d["calendar_id"] == "ghost_cal"
    # 重放同样拒绝
    _seed_calendar(db, "present", [(0, "G1", [[0, 100]])])
    _assert_anomaly(
        client.post("/plans/orphan/replay/present"),
        integrity.PLAN_CALENDAR_MISSING,
    )


def test_replay_corrupt_plan_is_anomaly_even_if_calendar_ok(client, db):
    _seed_calendar(db, "good_cal", [(0, "G1", [[0, 100]])])
    _seed_plan(db, "badp", "good_cal", "xxx", 10, _witnesses())
    _assert_anomaly(
        client.post("/plans/badp/replay/good_cal"),
        integrity.PLAN_PAYLOAD_NOT_JSON,
    )


def test_anomalies_are_deterministic_and_read_only(client, db):
    _seed_calendar(
        db,
        "corrupt",
        [(0, "G1", [[10, 20]]), (1, "G1", "bogus")],
    )

    def snapshot():
        return {
            "calendars": sorted(
                x.id for x in db.query(CalendarRow).all()
            ),
            "gates": sorted(
                (g.position, g.gate_id, g.windows)
                for g in db.query(GateRow)
                .filter(GateRow.calendar_id == "corrupt")
                .all()
            ),
            "plans": sorted(p.id for p in db.query(PlanRow).all()),
        }

    before = snapshot()
    bodies = []
    for _ in range(3):
        r = client.get("/calendars/corrupt")
        bodies.append(r.json())
        body = {**VOYAGE_BODY, "calendar_id": "corrupt"}
        bodies.append(client.post("/voyages/probe", json=body).json())
        body = {**PLAN_BODY, "calendar_id": "corrupt"}
        bodies.append(client.post("/plans", json=body).json())
    # 结果逐字节稳定
    for b in bodies[1:]:
        assert b == bodies[0]
    # 失败前后表内容逐项不变；且没有新增方案
    after = snapshot()
    assert after == before
    assert after["plans"] == []


def test_legal_legacy_record_works_unchanged(client, db):
    """直接预置的完整旧记录：区间、见证、重放与升级前一致。"""
    _seed_calendar(
        db,
        "legacy",
        [(0, "G1", [[10, 20]]), (1, "G2", [[25, 35]])],
    )
    cid = "legacy"
    resp = client.post(
        "/voyages/probe",
        json={
            "calendar_id": cid,
            "gates": ["G1", "G2"],
            "legs": [5],
            "max_waits": [5, 5],
            "search_start": 0,
            "search_end": 50,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["intervals"] == [[15, 20]]

    # 预置一个旧方案并读回
    witnesses = [
        {"gate_id": "G1", "arrival": 15, "entry": 15, "wait": 0},
        {"gate_id": "G2", "arrival": 20, "entry": 25, "wait": 5},
    ]
    _seed_plan(
        db,
        "legacy_plan",
        cid,
        _definition(("G1", "G2"), legs=[5], waits=[5, 5]),
        15,
        witnesses,
    )
    got = client.get("/plans/legacy_plan").json()
    assert got["witnesses"] == witnesses
    # 跨日历重放：新日历窗口右移 -> INVALID
    _seed_calendar(
        db,
        "legacy_new",
        [(0, "G1", [[10, 20]]), (1, "G2", [[26, 35]])],
    )
    body = client.post("/plans/legacy_plan/replay/legacy_new").json()
    assert body["status"] == "INVALID"
    assert body["failed_gate_id"] == "G2"
    assert body["arrival"] == 20
    assert body["wait_deadline"] == 25
    # 原方案未被改写
    assert client.get("/plans/legacy_plan").json()["witnesses"] == witnesses


def test_publish_semantics_unchanged(client):
    # 新发布日历仍走原有 422 校验，且成功后可正常读回
    resp = client.post(
        "/calendars",
        json={"gates": [{"gate_id": "G1", "windows": [[0, 10], [9, 20]]}]},
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    # FastAPI 请求体校验错误不带 DATA_ANOMALY 标记
    assert not (isinstance(detail, dict) and detail.get("error") == "DATA_ANOMALY")
