# 潮闸检修航程调度（Tide Gate Scheduling）

潮闸检修后，闸门只在断续的开放窗口可用。本服务根据调度员发布的闸门日历，
在出发搜索区间内求出**全部可行出发时刻的合并区间**，并在调度员选定时刻后
生成逐闸「最早可入」见证、落库为可重放的采纳方案。

- 后端：Python 3.12 + FastAPI
- 数据库：PostgreSQL（不可变日历 + 采纳方案）
- 部署：Docker Compose（API + 数据库），宿主机端口由 `API_PORT` 配置
- 测试：`./verify` 执行 pytest（算法穷举一致性 + API + 二十万窗口 ≤ 3 秒）

## 规则要点

- 闸门 ID 为不重复的非空白 ASCII 字符串（长度 1..128）。
- 所有时刻与时长均为 `[0, 10^12]` 内的整数秒。
- 每个窗口满足 `start < end`，按 `start` 升序、互不重叠但可相接；
  窗口与搜索区间一律**左闭右开**，恰在 `end` 入闸判为关闭。
- 航程含 1..200 个**互异**闸门、相邻航行时长、每闸最大等待时长、出发搜索区间。
- 首闸到达即出发；下一闸到达 = 本闸入闸 + 相邻航行时长。
- 最早可入原则：到达落在窗口内立即入闸，否则等待下一窗口；
  等待时长（入闸 − 到达）不得超过该闸最大等待。
- 无可行出发时刻时返回空区间列表。
- 日历与方案不可变；非法日历整版拒绝（HTTP 422）、不落库。

探测、采纳与重放遵守同一套半开窗口和最早可入规则。算法自末闸起逆向
传播「可行入闸时刻区间集」：多窗口闸门做一次线性合并扫描（窗口与区间
两个指针都不回退，密集区间下不退化为平方复杂度），**单窗口闸门走
bisect 快速路径**，中段区间整体引用、只裁剪首尾。典型稀疏分布
（末闸三万段稀疏窗口、前 199 闸各一段覆盖全程的窗口，合法窗口约
三万零二百）下，区间探测与二十万窗口场景一样在毫秒级完成，远低于
三秒上限；采纳单个可行时刻只是 O(闸数) 的前向模拟，立即成功。

## 启动

```bash
# 默认宿主端口 8000；可用 API_PORT 改端口
API_PORT=8080 docker compose up --build
```

健康检查：`GET /health` → `{"status":"ok"}`

交互式 API 文档：`http://localhost:8000/docs`

## 测试

```bash
./verify          # 需要本机 Python 3 且已安装 requirements-dev.txt
# 或
DATABASE_URL="sqlite://" python3 -m pytest -q
```

## 请求示例

### 1. 发布日历 `POST /calendars`（201）

```json
{
  "gates": [
    {"gate_id": "G1", "windows": [[10, 20], [30, 40]]},
    {"gate_id": "G2", "windows": [[25, 35]]}
  ]
}
```

响应返回 `id`（日历 ID）。窗口重叠、`start >= end`、ID 重复或含非 ASCII
字符都会返回 422 且不写入任何数据。`GET /calendars/{id}` 可读回。

### 2. 探测可行出发区间 `POST /voyages/probe`（200）

```json
{
  "calendar_id": "<calendar_id>",
  "gates": ["G1", "G2"],
  "legs": [5],
  "max_waits": [5, 5],
  "search_start": 0,
  "search_end": 50
}
```

- `legs` 长度 = 闸门数 − 1；`max_waits` 长度 = 闸门数。
- 响应：`{"intervals": [[15, 20]]}`，区间为左闭右开整数区间，已合并相接/重叠者。
- 无解：`{"intervals": []}`。
- 航程引用日历中不存在的闸门：422；日历不存在：404。

### 3. 只读比较两份日历 `POST /voyages/compare`（200）

输入两份既存日历与同一航程、等待上限及搜索区间，服务先分别按同一套
半开窗口规则求可行出发区间，再把整数秒时间轴切成最大连续差异片段：

```json
{
  "old_calendar_id": "<旧版日历 ID>",
  "new_calendar_id": "<新版日历 ID>",
  "gates": ["G1"],
  "legs": [],
  "max_waits": [3],
  "search_start": 0,
  "search_end": 50
}
```

响应保留两份来源区间，便于调用方复核：

```json
{
  "old_intervals": [[7, 20]],
  "new_intervals": [[10, 25]],
  "segments": [
    [7, 10, "仅旧版可行"],
    [10, 20, "两版均可行"],
    [20, 25, "仅新版可行"]
  ]
}
```

片段同样是左闭右开整数区间；两版均不可行的空隙不输出。只有标签相同且
端点相接的片段才会合并，跨越不可行空隙不会合并。任一日历损坏、航程数据
非法或航程引用缺闸时，整个请求返回 422，不返回一半结果；该接口只读，
不会创建或改写方案。任一日历 ID 不存在时返回 404。

### 4. 选定时刻并采纳 `POST /plans`（201 / 409）

```json
{
  "calendar_id": "<calendar_id>",
  "gates": ["G1", "G2"],
  "legs": [5],
  "max_waits": [5, 5],
  "departure": 15
}
```

成功返回方案 `id` 与逐闸见证（最早可入原则生成）：

```json
{
  "id": "<plan_id>",
  "departure": 15,
  "witnesses": [
    {"gate_id": "G1", "arrival": 15, "entry": 15, "wait": 0},
    {"gate_id": "G2", "arrival": 20, "entry": 25, "wait": 5}
  ]
}
```

时刻不可行返回 **409**，并指出首个失效闸门、到达时刻与等待截止时刻
（`arrival + max_wait`）及原因（`NO_OPEN_WINDOW` / `WAIT_EXCEEDED`）。
`GET /plans/{id}` 可读回方案。

### 5. 在新日历上重放方案 `POST /plans/{plan_id}/replay/{new_calendar_id}`

只读重放，**绝不改写原方案**。成功重放：

```json
{"status": "STILL_VALID", "failed_gate_id": null, "failed_index": null,
 "arrival": null, "wait_deadline": null, "reason": null}
```

失效时返回 `INVALID` 及**首个失效闸门**（按航程顺序单趟重放）、到达与
等待截止时刻；新日历缺少闸门时 `reason` 为 `GATE_NOT_IN_CALENDAR`，
`arrival` 为 `null`。若航程前段已因窗口关闭（`NO_OPEN_WINDOW`）或等待
超限（`WAIT_EXCEEDED`）失效、而后段某闸在新日历中缺失，接口报告
**首个实际失效闸门**及其到达与等待截止时刻——后段缺闸不会掩盖首因；
仅当前面各闸均成功时才可能报出缺闸。

## 数据与边界

- 日历、方案均不可变，系统不提供修改/删除接口；重放针对新日历进行。
- 恰在窗口 `end` 到达判为关闭，会尝试后续窗口；无后续窗口即 `NO_OPEN_WINDOW`。
- 窗口可相接（`[10,20)` 与 `[20,30)`），在 `20` 到达落入第二窗。

## 升级/恢复后读取既有记录

服务升级或从备份恢复后，调度员继续使用既有日历与方案。读取既有记录时
会做**整版完整性校验**，任何损坏都返回 HTTP **422** 数据异常，而不是
500、自相矛盾的详情或静默挑选某一行：

```json
{"detail": {"error": "DATA_ANOMALY", "type": "<稳定错误码>",
            "message": "...", "...": "定位上下文"}}
```

- 日历异常类型：`CALENDAR_NO_GATES`、`CALENDAR_DUPLICATE_POSITION`、
  `CALENDAR_DUPLICATE_GATE_ID`、`CALENDAR_GATE_WINDOWS_NOT_JSON`、
  `CALENDAR_WINDOWS_MALFORMED`、`CALENDAR_WINDOW_OUT_OF_DOMAIN`、
  `CALENDAR_WINDOW_INVERTED`、`CALENDAR_WINDOWS_OVERLAP`。
- 方案异常类型：`PLAN_PAYLOAD_NOT_JSON`、`PLAN_DEFINITION_MALFORMED`、
  `PLAN_WITNESSES_NOT_JSON`、`PLAN_WITNESSES_SHAPE_MALFORMED`、
  `PLAN_WITNESS_MISMATCH`、`PLAN_WITNESS_SEMANTIC_MISMATCH`、
  `PLAN_CALENDAR_MISSING`。

见证校验分两层。**结构层**：见证数组形状、长度、字段类型、闸门次序、
`wait == entry − arrival` 的逐项算术。**语义层**：方案必须是原日历上的
合法采纳快照——首闸到达等于出发时刻、后闸到达等于前闸入闸加航行时长、
每个入闸时刻都是原日历按最早可入原则得到的时刻（恰在 `end` 到达判为
关闭）、等待不超过该闸上限。即使见证数组形状与每项等待算术都合法，
到达递推对不上或入闸不是最早开放时刻，也返回稳定的
`PLAN_WITNESS_SEMANTIC_MISMATCH`（上下文含 `witness_index`、期望/实际
值），查询与重放都不接受这一不可信来源；方案引用的原日历缺失则为
`PLAN_CALENDAR_MISSING`。结构/语义均合法的旧方案继续可读、可重放。

同一响应体在重复查询与服务重启后保持一致；这些路径**只读**，不新增方案、
不改写任何日历、方案与见证。新发布日历的整版原子 422 拒绝与上述旧记录
数据异常相互区分（请求体校验错误不含 `DATA_ANOMALY` 标记）。

真实 PostgreSQL 验收（预置坏数据、重复查询、重启服务、表内容逐项快照
比对、合法旧记录跨日历重放）：

```bash
PG_ACCEPTANCE=1 \
PG_ACCEPTANCE_URL="postgresql+psycopg://tide@localhost:5432/tide_acceptance" \
python3 -m pytest tests/test_postgres_acceptance.py -s
```
