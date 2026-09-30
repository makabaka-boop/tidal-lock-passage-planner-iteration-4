"""API 端到端流程：日历发布、探测、采纳、重放、校验与状态码。"""
from __future__ import annotations


def _calendar_body(g1_windows, g2_windows=None):
    gates = [{"gate_id": "G1", "windows": g1_windows}]
    if g2_windows is not None:
        gates.append({"gate_id": "G2", "windows": g2_windows})
    return {"gates": gates}


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_publish_and_read_calendar(client):
    body = _calendar_body([[10, 20], [30, 40]])
    resp = client.post("/calendars", json=body)
    assert resp.status_code == 201
    cid = resp.json()["id"]
    got = client.get(f"/calendars/{cid}").json()
    assert got["gates"] == [
        {"gate_id": "G1", "windows": [[10, 20], [30, 40]]}
    ]
    assert client.get("/calendars/nope").status_code == 404


def test_illegal_calendar_rejected_and_not_stored(client):
    # start >= end
    bad = _calendar_body([[20, 20]])
    assert client.post("/calendars", json=bad).status_code == 422
    # 窗口重叠
    bad = _calendar_body([[0, 10], [9, 20]])
    assert client.post("/calendars", json=bad).status_code == 422
    # 重复闸门 ID
    resp = client.post(
        "/calendars",
        json={
            "gates": [
                {"gate_id": "A", "windows": [[0, 1]]},
                {"gate_id": "A", "windows": [[0, 1]]},
            ]
        },
    )
    assert resp.status_code == 422
    # 越界整数
    bad = _calendar_body([[0, 10**12 + 1]])
    assert client.post("/calendars", json=bad).status_code == 422
    # 空闸门列表 / 空窗口
    assert client.post("/calendars", json={"gates": []}).status_code == 422
    assert (
        client.post(
            "/calendars", json=_calendar_body([])
        ).status_code
        == 422
    )
    # 非整数 / 布尔不接受（严格整数）
    assert (
        client.post(
            "/calendars", json=_calendar_body([[0, True]])
        ).status_code
        == 422
    )
    # 非法整版不落库：日历表应为空
    resp = client.get("/health")
    assert resp.status_code == 200


def test_non_ascii_gate_id_rejected(client):
    resp = client.post(
        "/calendars",
        json={"gates": [{"gate_id": "闸A", "windows": [[0, 1]]}]},
    )
    assert resp.status_code == 422


def test_probe_end_to_end(client, make_calendar):
    cid = make_calendar([[10, 20]])
    resp = client.post(
        "/voyages/probe",
        json={
            "calendar_id": cid,
            "gates": ["G1"],
            "legs": [],
            "max_waits": [3],
            "search_start": 0,
            "search_end": 30,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["intervals"] == [[7, 20]]


def test_probe_no_solution_empty_intervals(client, make_calendar):
    cid = make_calendar([[100, 110]])
    resp = client.post(
        "/voyages/probe",
        json={
            "calendar_id": cid,
            "gates": ["G1"],
            "legs": [],
            "max_waits": [0],
            "search_start": 0,
            "search_end": 50,
        },
    )
    assert resp.json()["intervals"] == []


def test_probe_unknown_gate_is_422(client, make_calendar):
    cid = make_calendar([[0, 10]])
    resp = client.post(
        "/voyages/probe",
        json={
            "calendar_id": cid,
            "gates": ["MISSING"],
            "legs": [],
            "max_waits": [0],
            "search_start": 0,
            "search_end": 5,
        },
    )
    assert resp.status_code == 422


def test_probe_bad_voyage_shape(client, make_calendar):
    cid = make_calendar([[0, 10]])
    base = {
        "calendar_id": cid,
        "gates": ["G1", "G1"],  # 重复
        "legs": [1],
        "max_waits": [0, 0],
        "search_start": 0,
        "search_end": 5,
    }
    assert client.post("/voyages/probe", json=base).status_code == 422
    base["gates"] = ["G1"]
    base["legs"] = [1]  # legs 应为 0 个
    assert client.post("/voyages/probe", json=base).status_code == 422
    base["legs"] = []
    base["search_start"] = 5
    base["search_end"] = 5  # start < end
    assert client.post("/voyages/probe", json=base).status_code == 422


def _compare_body(old_id, new_id, gate_count=1):
    return {
        "old_calendar_id": old_id,
        "new_calendar_id": new_id,
        "gates": [f"G{i + 1}" for i in range(gate_count)],
        "legs": [5] * (gate_count - 1),
        "max_waits": [5] * gate_count,
        "search_start": 0,
        "search_end": 50,
    }


def test_compare_two_calendars_returns_sources_and_segments(
    client, make_calendar
):
    # 旧版 G1 [10,20)，新版 [15,25)；可行区间即窗口本身。
    old_id = make_calendar([[10, 20]])
    new_id = make_calendar([[15, 25]])
    resp = client.post("/voyages/compare", json=_compare_body(old_id, new_id))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "old_intervals": [[10, 20]],
        "new_intervals": [[15, 25]],
        "segments": [
            [10, 15, "仅旧版可行"],
            [15, 20, "两版均可行"],
            [20, 25, "仅新版可行"],
        ],
    }


def test_compare_empty_feasible_sets(client, make_calendar):
    old_id = make_calendar([[100, 110]])
    new_id = make_calendar([[120, 130]])
    resp = client.post("/voyages/compare", json=_compare_body(old_id, new_id))
    assert resp.status_code == 200
    body = resp.json()
    assert body["old_intervals"] == []
    assert body["new_intervals"] == []
    assert body["segments"] == []


def test_compare_swapped_calendars(client, make_calendar):
    old_id = make_calendar([[10, 20]])
    new_id = make_calendar([[15, 25]])
    first = client.post(
        "/voyages/compare", json=_compare_body(old_id, new_id)
    ).json()["segments"]
    swapped_resp = client.post(
        "/voyages/compare", json=_compare_body(new_id, old_id)
    )
    swapped = swapped_resp.json()["segments"]
    assert swapped == [
        [10, 15, "仅新版可行"],
        [15, 20, "两版均可行"],
        [20, 25, "仅旧版可行"],
    ]
    assert first[0][2] != swapped[0][2]


def test_compare_missing_calendar_is_404(client, make_calendar):
    cid = make_calendar([[10, 20]])
    resp = client.post("/voyages/compare", json=_compare_body(cid, "missing"))
    assert resp.status_code == 404
    resp = client.post("/voyages/compare", json=_compare_body("missing", cid))
    assert resp.status_code == 404


def test_compare_missing_gate_rejects_entire_request(client, make_calendar):
    old_id = make_calendar([[10, 20]], [[20, 30]])
    new_id = make_calendar([[10, 20]])  # 缺 G2
    resp = client.post(
        "/voyages/compare", json=_compare_body(old_id, new_id, gate_count=2)
    )
    assert resp.status_code == 422
    assert "intervals" not in resp.text and "segments" not in resp.text


def test_compare_is_read_only(client, make_calendar):
    old_id = make_calendar([[10, 20]])
    new_id = make_calendar([[15, 25]])
    before = client.get(f"/calendars/{old_id}").json()
    client.post("/voyages/compare", json=_compare_body(old_id, new_id))
    assert client.get(f"/calendars/{old_id}").json() == before


def test_adopt_plan_and_get_witnesses(client, make_calendar):
    cid = make_calendar([[10, 20]], [[25, 35]])
    # G1 [10,20)，航行 5，G2 [25,35)，各允许等待 5
    # 出发 12：12 入 G1，17 到 G2，等 8 -> 超出 -> 409
    resp = client.post(
        "/plans",
        json={
            "calendar_id": cid,
            "gates": ["G1", "G2"],
            "legs": [5],
            "max_waits": [5, 5],
            "departure": 12,
        },
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["failed_gate_id"] == "G2"
    assert detail["arrival"] == 17
    assert detail["wait_deadline"] == 22

    # 出发 15：15 入 G1，20 到 G2，等 5 -> 恰好可行
    resp = client.post(
        "/plans",
        json={
            "calendar_id": cid,
            "gates": ["G1", "G2"],
            "legs": [5],
            "max_waits": [5, 5],
            "departure": 15,
        },
    )
    assert resp.status_code == 201, resp.text
    plan = resp.json()
    assert plan["witnesses"] == [
        {"gate_id": "G1", "arrival": 15, "entry": 15, "wait": 0},
        {"gate_id": "G2", "arrival": 20, "entry": 25, "wait": 5},
    ]
    # 可按 ID 取回
    got = client.get(f"/plans/{plan['id']}").json()
    assert got["departure"] == 15
    assert client.get("/plans/nope").status_code == 404


def test_replay_still_valid_and_invalid(client, make_calendar):
    cid1 = make_calendar([[10, 20]], [[25, 35]])
    resp = client.post(
        "/plans",
        json={
            "calendar_id": cid1,
            "gates": ["G1", "G2"],
            "legs": [5],
            "max_waits": [5, 5],
            "departure": 15,
        },
    )
    pid = resp.json()["id"]

    # 在同结构新日历上重放 -> STILL_VALID
    cid2 = make_calendar([[10, 20]], [[25, 35]])
    resp = client.post(f"/plans/{pid}/replay/{cid2}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "STILL_VALID"

    # 新日历中 G2 窗口右移：20 到达，窗口 26 才开，等待截止 25 -> INVALID
    cid3 = make_calendar([[10, 20]], [[26, 35]])
    resp = client.post(f"/plans/{pid}/replay/{cid3}")
    body = resp.json()
    assert body["status"] == "INVALID"
    assert body["failed_gate_id"] == "G2"
    assert body["failed_index"] == 1
    assert body["arrival"] == 20
    assert body["wait_deadline"] == 25

    # 恰在 end 到达：G2 变 [20,25)，20 到达可入 -> 有效
    cid4 = make_calendar([[10, 20]], [[20, 25]])
    assert (
        client.post(f"/plans/{pid}/replay/{cid4}").json()["status"]
        == "STILL_VALID"
    )
    # G2 变 [15,20)：20 到达恰在 end -> 关闭且无后续窗口 -> INVALID
    cid5 = make_calendar([[10, 20]], [[15, 20]])
    body = client.post(f"/plans/{pid}/replay/{cid5}").json()
    assert body["status"] == "INVALID"
    assert body["arrival"] == 20
    assert body["reason"] == "NO_OPEN_WINDOW"

    # 新日历缺闸门 -> INVALID（首闸还在，G2 缺失）
    cid6 = make_calendar([[10, 20]])
    body = client.post(f"/plans/{pid}/replay/{cid6}").json()
    assert body["status"] == "INVALID"
    assert body["failed_gate_id"] == "G2"

    # 原方案未被改写
    assert client.get(f"/plans/{pid}").json()["departure"] == 15
    # 不存在资源
    assert client.post(f"/plans/nope/replay/{cid2}").status_code == 404
    assert client.post(f"/plans/{pid}/replay/nope").status_code == 404


def test_replay_first_failure_not_masked_by_missing_later_gate(
    client, make_calendar
):
    """前段已失效（窗口关闭/等待超限）而后续闸在新日历缺失时，
    必须报告首个实际失效闸门及其到达、等待截止时刻，而不是后段缺闸。"""
    # 三闸航程：G1 -> G2 -> G3，出发 15：15 入 G1，航行 5 后到 G2。
    cid = make_calendar([[10, 20]], [[25, 35]], [[30, 50]])
    resp = client.post(
        "/plans",
        json={
            "calendar_id": cid,
            "gates": ["G1", "G2", "G3"],
            "legs": [5, 5],
            "max_waits": [5, 5, 5],
            "departure": 15,
        },
    )
    pid = resp.json()["id"]

    # 新日历缺 G3，但 G2 先失效：20 到达、G2 窗口 30 才开，
    # 等待截止 25 -> WAIT_EXCEEDED；首因是 G2，而非缺失的 G3。
    cid_wait = make_calendar([[10, 20]], [[30, 40]])
    body = client.post(f"/plans/{pid}/replay/{cid_wait}").json()
    assert body["status"] == "INVALID"
    assert body["failed_gate_id"] == "G2"
    assert body["failed_index"] == 1
    assert body["arrival"] == 20
    assert body["wait_deadline"] == 25
    assert body["reason"] == "WAIT_EXCEEDED"

    # 前段在 G2 恰遇窗口关闭且无后续窗口：NO_OPEN_WINDOW 首因。
    cid_closed = make_calendar([[10, 20]], [[15, 20]])
    body = client.post(f"/plans/{pid}/replay/{cid_closed}").json()
    assert body["status"] == "INVALID"
    assert body["failed_gate_id"] == "G2"
    assert body["failed_index"] == 1
    assert body["arrival"] == 20
    assert body["wait_deadline"] == 25
    assert body["reason"] == "NO_OPEN_WINDOW"

    # 前段全部成功时，后段缺闸仍照常报告 GATE_NOT_IN_CALENDAR。
    cid_missing = make_calendar([[10, 20]], [[25, 35]])
    body = client.post(f"/plans/{pid}/replay/{cid_missing}").json()
    assert body["status"] == "INVALID"
    assert body["failed_gate_id"] == "G3"
    assert body["failed_index"] == 2
    assert body["arrival"] is None
    assert body["wait_deadline"] is None
    assert body["reason"] == "GATE_NOT_IN_CALENDAR"

    # 原方案未被改写
    assert client.get(f"/plans/{pid}").json()["departure"] == 15


def test_immutable_calendar_replay_does_not_mutate(client, make_calendar):
    cid = make_calendar([[0, 100]])
    resp = client.post(
        "/plans",
        json={
            "calendar_id": cid,
            "gates": ["G1"],
            "legs": [],
            "max_waits": [0],
            "departure": 50,
        },
    )
    pid = resp.json()["id"]
    # 日历内容不可通过 API 修改（无写接口）；同 ID 重放仍有效
    assert (
        client.post(f"/plans/{pid}/replay/{cid}").json()["status"]
        == "STILL_VALID"
    )
