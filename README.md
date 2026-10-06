# 博物馆展厅灯光场景编排 API

为博物馆展厅编排**可定时切换的灯光场景**的 HTTP 服务。输入灯具（通道）与场景（亮度、渐变时长、生效时间窗），服务会：

1. **校验**
   - 场景引用的灯具必须存在（`unknown_lamp`）；
   - 亮度必须在 **0–100**（百分比，对应 DMX 0–255）范围内（`level_out_of_range`）；
   - 场景时间窗合法（`end_at > start_at`，`invalid_time_window`）、同一灯具在一个场景内只能出现一次（`duplicate_channel_in_scene`）；
   - **同一通道上，普通场景的渐变区间 `[start_at, start_at + fade_seconds)` 不得重叠**（端点相接合法），冲突返回具体通道、时间区间和涉及的两个场景（`fade_interval_overlap`）；
   - 紧急场景彼此的生效时间窗不得重叠（`emergency_overlap`）。
2. **合成可执行时间线**：按通道输出排序后的指令序列（`fade` / `emergency_enter` / `emergency_exit`），每条指令带 `at`、`from_level`、`to_level`、`fade_end_at`，可直接下发 DMX/调光网关。
3. **紧急抢占**：紧急场景（`priority: "emergency"`）在 `start_at` 立即抢占普通场景；在 `end_at` 释放通道时，**只恢复到"抢占前已经开始、且释放时仍在生效时间窗内"的那个普通场景**；在紧急期间才到点的普通场景标记为 `skipped`，不会被恢复执行。
4. 校验失败返回 **HTTP 422** 和结构化冲突明细（通道/区间/原因），**不保存无效编排**。

---

## 运行

### Docker Compose（推荐）

```bash
docker compose up --build
# 服务监听 http://localhost:8080
```

### 本地 Python

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

打开交互文档：<http://localhost:8080/docs>（Swagger UI）、<http://localhost:8080/redoc>。

### 测试

```bash
pip install pytest httpx
pytest -q
```

---

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查 |
| `PUT` | `/halls/{hall_id}` | 校验并全量保存一个展厅编排（成功 200，冲突 422 且不写入） |
| `POST` | `/halls/batch-publish` | **一次原子发布多个展厅编排**（全成功或全不写入，乐观版本控制） |
| `POST` | `/validate` | 只校验/试编译（dry run），不落库 |
| `GET` | `/halls` | 列出已保存展厅 |
| `GET` | `/halls/{hall_id}` | 获取编译后的完整计划 |
| `GET` | `/halls/{hall_id}/timeline` | 只取每通道可执行时间线 |
| `GET` | `/halls/{hall_id}/state?at=...` | 模拟某时刻各通道的实际亮度/生效场景 |
| `DELETE` | `/halls/{hall_id}` | 删除编排 |
| `POST` | `/halls/{hall_id}/dispatch/claim` | **网关按通道领取到期指令**（租约制，顺序交付） |
| `POST` | `/halls/{hall_id}/dispatch/ack` | **网关确认已领取任务**（匹配租约令牌，幂等） |

数据为**内存存储**（线程安全，单 worker），重启清空；派发队列与租约状态（`app/dispatch.py`）同为内存态并随存储清空。如需多实例持久化可将 `app/store.py` 替换为 Redis/数据库实现，`app/engine.py` 是纯函数、与存储无关。

### 数据模型要点

- `lamp`：`id` 即通道号；`default_level` 是无场景生效时的保持亮度。
- `scene`：
  - `priority`：`normal`（默认）或 `emergency`；
  - `start_at` / `end_at`：ISO 8601（建议带 `Z` 或时区偏移，无时区按 UTC 处理），区间左闭右开；
  - `items[]`：`lamp_id` + `level`(0–100) + `fade_seconds`（渐变时长，0 为瞬切）；紧急项可选 `restore_fade_seconds` 指定释放回普通场景时的渐变时长（默认沿用 `fade_seconds`）。

---

## 调用示例

示例文件：[`examples/bronze-gallery.json`](examples/bronze-gallery.json)。

### 1. 提交编排（校验通过 → 保存并返回时间线）

```bash
curl -sS -X PUT http://localhost:8080/halls/bronze-gallery \
  -H 'Content-Type: application/json' \
  --data @examples/bronze-gallery.json | jq
```

响应节选（`timeline` 为每个通道的指令序列；`preemptions` 描述每次抢占与恢复；`skipped_activations` 是被紧急场景跳过的普通场景）：

```json
{
  "hall_id": "bronze-gallery",
  "version": 1,
  "summary": {"lamps": 3, "scenes": 4, "normal_scenes": 3, "emergency_scenes": 1, "commands": 13, "preemptions": 2, "skipped_scene_activations": 0},
  "timeline": {
    "lamp-case-1": [
      {"type": "fade", "at": "2026-10-06T08:00:00+00:00", "from_level": 0, "to_level": 60.0, "fade_end_at": "2026-10-06T08:05:00+00:00", "fade_seconds": 300.0, "scene_id": "morning-open", "priority": "normal"},
      {"type": "emergency_enter", "at": "2026-10-06T10:00:00+00:00", "from_level": 60.0, "to_level": 5.0, "fade_end_at": "2026-10-06T10:00:00+00:00", "fade_seconds": 0.0, "scene_id": "conservation-alert", "priority": "emergency"},
      {"type": "emergency_exit", "at": "2026-10-06T10:30:00+00:00", "from_level": 5.0, "to_level": 60.0, "fade_end_at": "2026-10-06T10:33:00+00:00", "fade_seconds": 180.0, "scene_id": "conservation-alert", "priority": "emergency", "restored_scene_id": "morning-open"}
    ]
  },
  "preemptions": [
    {"emergency_scene_id": "conservation-alert", "channel": "lamp-case-1", "from": "2026-10-06T10:00:00+00:00", "to": "2026-10-06T10:30:00+00:00", "restored_scene_id": "morning-open", "restored_level": 60.0, "skipped_scene_ids": []}
  ]
}
```

说明：10:00 紧急场景把展柜灯从 **60%** 瞬切到保护值 **5%**；10:30 释放时，`morning-open`（08:00–12:00）在抢占前已开始且仍有效，因此用 180s 渐变恢复到 **60%**。若普通场景此时正在渐变中途，恢复目标是按抢占时刻插值出的亮度。

### 2. 查询某时刻的实际输出

```bash
# 紧急期间
curl -sS "http://localhost:8080/halls/bronze-gallery/state?at=2026-10-06T10:10:00Z" | jq '.channels'
# => { "lamp-case-1": {"level": 5, "active_scene_id": "conservation-alert", "priority": "emergency", "in_emergency": true},
#      "lamp-wash":   {"level": 35, "active_scene_id": "morning-open",       "priority": "normal",    "in_emergency": false}, ... }
# 注意：紧急场景只引用了两盏展柜灯，wall wash 通道不受影响，继续运行普通场景。

# 释放恢复后
curl -sS "http://localhost:8080/halls/bronze-gallery/state?at=2026-10-06T10:40:00Z" | jq '.channels."lamp-case-1"'
# => {"level": 60, "active_scene_id": "morning-open", ...}
```

### 3. 冲突：同一通道渐变区间重叠 → 422，且不保存

```bash
curl -sS -X POST http://localhost:8080/validate -H 'Content-Type: application/json' -d '{
  "id": "bad",
  "lamps": [{"id": "L1"}],
  "scenes": [
    {"id": "a", "start_at": "2026-10-06T08:00:00Z", "end_at": "2026-10-06T20:00:00Z",
     "items": [{"lamp_id": "L1", "level": 50, "fade_seconds": 600}]},
    {"id": "b", "start_at": "2026-10-06T08:09:59Z", "end_at": "2026-10-06T20:00:00Z",
     "items": [{"lamp_id": "L1", "level": 70, "fade_seconds": 10}]}
  ]
}'
```

```json
{
  "ok": false,
  "hall_id": "bad",
  "message": "Orchestration rejected; no timeline was saved.",
  "errors": [
    {
      "code": "fade_interval_overlap",
      "message": "Fade intervals on channel 'L1' overlap for scenes 'a' and 'b' between 2026-10-06T08:09:59+00:00 and 2026-10-06T08:10:00+00:00.",
      "scene_id": "b",
      "channel": "L1",
      "interval": ["2026-10-06T08:09:59+00:00", "2026-10-06T08:10:00+00:00"],
      "between_scenes": ["a", "b"]
    }
  ],
  "warnings": []
}
```

`a` 的渐变在 08:10:00 结束，`b` 在 08:09:59 开始——半开区间相交 1 秒即拒绝；若 `b` 从 **08:10:00** 开始则合法（端点相接）。此时 `GET /halls/bad` 返回 404，证明未保存。

### 4. 冲突：引用不存在的灯具 / 亮度越界

```bash
curl -sS -X PUT http://localhost:8080/halls/bad2 -H 'Content-Type: application/json' -d '{
  "id": "bad2",
  "lamps": [{"id": "L1"}],
  "scenes": [
    {"id": "s", "start_at": "2026-10-06T08:00:00Z", "end_at": "2026-10-06T09:00:00Z",
     "items": [{"lamp_id": "L9", "level": 120, "fade_seconds": 0}]}
  ]
}'
```

- `level: 120` 由 Pydantic 以 422 拦截（字段级错误）；
- 改为合法亮度但引用未声明灯具时，返回业务错误 `unknown_lamp`，同样 422 且不写入。

### 5. 抢占恢复边界语义

- 场景 B 在紧急窗口**之内**到点：B 出现在 `skipped_activations` 中，释放时不会执行 B，只恢复抢占前仍有效的场景。
- 抢占前已开始、但在释放时刻**之前结束**的场景不可恢复：`emergency_exit.restored_scene_id` 为 `null`，通道保持紧急亮度直到下一个已排定场景接管，指令带 `note` 说明。
- 新普通场景恰好在释放的同一时刻开始：它直接接管通道，`emergency_exit` 带 `taken_over_by`，不产生可见的亮度跳变。
- 紧急场景只影响自己 `items` 中列出的通道，其他通道时间线完全不变。

---

## 批量发布多个展厅（原子提交）

运营一次发布**多个展厅的完整灯光编排**，用 `POST /halls/batch-publish`。请求体是一个**非空**列表 `halls[]`，每项：

| 字段 | 说明 |
| --- | --- |
| `hall_id` | 目标展厅 ID（同一批内不得重复） |
| `expected_version` | 乐观锁：你认为当前正在替换的版本；**尚未保存的新展厅传 `null`** |
| `orchestration` | 该展厅的完整编排，结构与 `PUT /halls/{hall_id}` 的请求体完全一致 |

语义保证：

- **逐项沿用**单展厅的校验与时间线合成规则（灯具引用、亮度范围、渐变区间不重叠、紧急抢占等）。
- 任一项出现下列问题时，**整批拒绝、不写入任何展厅，所有版本保持不变**，响应里给出每个问题展厅的 `index`、`hall_id` 与 `reason`：
  - `duplicate_in_batch`（同批 ID 重复，422）；
  - `id_mismatch`（`hall_id` 与 `orchestration.id` 不符，422）；
  - `orchestration_invalid`（编排未通过校验，422，`reason.errors` 为单展厅同款结构化冲突明细）；
  - `version_mismatch`（`expected_version` 与当前版本不符，**409**，返回 `expected_version` 与 `current_version`）。一批中既有内容错误又有版本冲突时，状态码取 **409** 且所有问题一次返回。
- 全部通过时**原子替换整批展厅**，每个展厅版本恰好 **+1**（新建为 `1`），按提交顺序返回各自的新版本、完整计划与时间线。
- **并发**发布涉及同一展厅且使用**相同 `expected_version`** 时，至多一方成功（另一方得到 409，需重新 `GET` 最新版本后再发布）。版本比对与整批写入在同一临界区完成。
- 原有单展厅 HTTP API（`PUT`/`GET`/`DELETE` 等）与响应结构保持不变、继续可用。

示例文件：[`examples/batch-publish.json`](examples/batch-publish.json)（第 1 项更新已存在的 `bronze-gallery`（期望版本 1），第 2 项以 `null` 创建新展厅 `porcelain-hall`）。

### 6. 批量发布：成功（各自版本 +1，返回新版本与时间线）

```bash
# 先放入 bronze-gallery，使其处于版本 1
curl -sS -X PUT http://localhost:8080/halls/bronze-gallery \
  -H 'Content-Type: application/json' --data @examples/bronze-gallery.json >/dev/null

# 一次发布一批：更新 bronze-gallery（expected_version=1）+ 新建 porcelain-hall（null）
curl -sS -X POST http://localhost:8080/halls/batch-publish \
  -H 'Content-Type: application/json' \
  --data @examples/batch-publish.json | jq '{ok, count, versions: [.published[] | {hall_id, version, lamps: .summary.lamps, commands: .summary.commands}]}'
```

```json
{
  "ok": true,
  "count": 2,
  "versions": [
    {"hall_id": "bronze-gallery", "version": 2, "lamps": 3, "commands": 13},
    {"hall_id": "porcelain-hall",  "version": 1, "lamps": 2, "commands": 4}
  ]
}
```

`published[]` 每一项就是单展厅 `GET /halls/{hall_id}` 的完整响应（含 `timeline`、`preemptions`、`skipped_activations`、`warnings` 等）。

### 7. 批量发布：版本冲突 → 409，整批不写入

```bash
# bronze-gallery 已被别人更新到版本 2，本批仍按版本 1 提交
curl -sS -X POST http://localhost:8080/halls/batch-publish \
  -H 'Content-Type: application/json' --data @examples/batch-publish.json
```

```json
{
  "ok": false,
  "message": "Batch publish rejected; no hall was written and all versions are unchanged.",
  "failures": [
    {
      "index": 0,
      "hall_id": "bronze-gallery",
      "reason": {
        "code": "version_mismatch",
        "hall_id": "bronze-gallery",
        "message": "Expected hall 'bronze-gallery' at version 1, but current version is 2.",
        "expected_version": 1,
        "current_version": 2
      }
    }
  ]
}
```

此时同批的 `porcelain-hall` **也不会被创建**（整批原子）；`GET /halls/porcelain-hall` 返回 404，`bronze-gallery` 仍是版本 2。调用方应先 `GET` 取到最新版本、按最新编排重新校验后再发布。

> 新建展厅请传 `expected_version: null`；若该 ID 已存在，会得到 `expected_version=null / current_version=<n>` 的 409。对不存在的展厅传非 `null` 版本，同样得到 409（`current_version: null`）。

### 8. 批量发布：内容错误（ID 不符 / 编排无效 / 同批重复）→ 422

整批中不同展厅可以同时出错，响应一次性列出，各自带 `index` 便于定位：

```json
{
  "ok": false,
  "message": "Batch publish rejected; no hall was written and all versions are unchanged.",
  "failures": [
    {"index": 0, "hall_id": "hall-x", "reason": {"code": "id_mismatch", "message": "Item hall id 'hall-x' does not match orchestration id 'different-id'."}},
    {"index": 1, "hall_id": "hall-y", "reason": {"code": "orchestration_invalid", "message": "Orchestration rejected; no timeline was saved.",
        "errors": [{"code": "fade_interval_overlap", "channel": "L1", "between_scenes": ["a", "b"], "...": "..."}], "warnings": []}}
  ]
}
```

空列表 `{"halls": []}`、缺字段、亮度越界等请求体形状错误由 Pydantic 直接返回 422（与单展厅一致），同样不写入。

### 9. 并发发布相同预期版本：至多一方成功

版本比较与整批写入在**同一个锁临界区**内完成。两个运营人员同时对版本 1 的同一展厅发起批量发布（都带 `expected_version: 1`）时：一方 `200`（版本变为 2），另一方 `409`（`current_version: 2`、其整批不写入）。失败方重新拉取版本 2 的当前编排、合并自己的修改后再提交即可。

---

## 展厅网关任务派发（领取 / 确认）

场馆网关从**当前已发布版本**按通道领取到期指令、就地执行、再凭租约令牌确认。两个端点：

- `POST /halls/{hall_id}/dispatch/claim` —— 领取。请求体：
  - `channel`（可选）：只领该通道；**缺省时跨所有通道**各取队首一个任务；
  - `limit`（必填，≥1）：本次领取数量上限；
  - `lease_seconds`（必填，≥0）：租约时长（秒），`0` 表示立即过期。
- `POST /halls/{hall_id}/dispatch/ack` —— 确认。请求体：`task_id` + `lease_token`。

语义规则：

1. **顺序交付**：每通道严格按执行时间 `at` 升序交付；**前一任务未确认前，后一任务不可领取**，因此单通道一次至多返回 1 个任务（跨通道领取时每通道至多 1 个、总数不超过 `limit`）。
2. **到期才发**：只发放 `at <= 当前时间` 的指令；队首未到期则该通道本轮没有可领任务。
3. **租约互斥**：租约内同一任务**不会重复发放**；租约过期后**同一 `task_id` 可重领并换发新令牌**（`task_id` 稳定不变）。
4. **确认匹配当前令牌**：重复确认（同一 `task_id` + 同一令牌）**幂等**返回 200（`duplicate: true`）；旧令牌/错令牌返回 **409 `token_conflict`** 且不改变任何状态。
5. **版本失效**：展厅**重新发布或删除**后，旧版本未确认任务全部失效——旧租约确认返回 **409 `version_expired`**（删除时 `current_version: null`）；**新版本不补发其发布时间之前的指令**（首版发放全部指令）。单展厅 `PUT` 与批量发布遵循同一规则；**校验失败或版本冲突的发布不改变队列**。删除后重建同名展厅也属于新一代：旧租约确认同样报告 `version_expired`（`task_id` 中的 `g<n>` 为发布代次，随每次发布/删除递增，不会与新一代撞号）。
6. **并发安全**：领取、确认与发布（含批量）在同一锁临界区内串行，**不会出现跨版本确认**。

### 10. 网关领取到期指令

```bash
curl -sS -X PUT http://localhost:8080/halls/bronze-gallery \
  -H 'Content-Type: application/json' --data @examples/bronze-gallery.json >/dev/null

# 按通道领取（也可省略 channel 跨通道领取，limit 限制总数）
curl -sS -X POST http://localhost:8080/halls/bronze-gallery/dispatch/claim \
  -H 'Content-Type: application/json' \
  -d '{"channel": "lamp-case-1", "limit": 5, "lease_seconds": 30}' | jq
```

```json
{
  "hall_id": "bronze-gallery",
  "version": 1,
  "count": 1,
  "tasks": [
    {
      "task_id": "bronze-gallery:g1:v1:lamp-case-1:0000",
      "hall_id": "bronze-gallery",
      "version": 1,
      "channel": "lamp-case-1",
      "command": {"type": "fade", "at": "2026-10-06T08:00:00+00:00", "from_level": 0.0, "to_level": 60.0, "fade_end_at": "2026-10-06T08:05:00+00:00", "fade_seconds": 300.0, "scene_id": "morning-open", "priority": "normal"},
      "lease_token": "sx1LvyI-dQzOFVR1lYN1dw",
      "lease_expires_at": "2026-10-06T09:20:06.367705+00:00"
    }
  ]
}
```

`command` 就是编译后时间线里的原始指令，可直接下发调光网关。前一任务未确认时再次领取返回空列表：

```bash
curl -sS -X POST http://localhost:8080/halls/bronze-gallery/dispatch/claim \
  -H 'Content-Type: application/json' \
  -d '{"channel": "lamp-case-1", "limit": 5, "lease_seconds": 30}' | jq -c '{count, tasks}'
# => {"count":0,"tasks":[]}     （租约未过期 + 前一任务未确认）
```

### 11. 确认与重复确认（幂等）

```bash
TOKEN=sx1LvyI-dQzOFVR1lYN1dw
curl -sS -X POST http://localhost:8080/halls/bronze-gallery/dispatch/ack \
  -H 'Content-Type: application/json' \
  -d "{\"task_id\": \"bronze-gallery:g1:v1:lamp-case-1:0000\", \"lease_token\": \"$TOKEN\"}" | jq
```

```json
{"ok": true, "hall_id": "bronze-gallery", "task_id": "bronze-gallery:g1:v1:lamp-case-1:0000", "version": 1, "channel": "lamp-case-1", "acknowledged": true, "duplicate": false}
```

同一请求重放仍返回 200（`duplicate: true`），游标只前进一次；确认后该通道的下一个到期指令才可领取。

### 12. 租约过期重领 + 旧令牌冲突

```bash
# 用 0 秒租约模拟立即过期（实际部署用正常时长）
curl -sS -X POST http://localhost:8080/halls/bronze-gallery/dispatch/claim \
  -H 'Content-Type: application/json' -d '{"channel": "lamp-case-2", "limit": 1, "lease_seconds": 0}' | jq -r '.tasks[0].lease_token'
# => OLD_TOKEN
curl -sS -X POST http://localhost:8080/halls/bronze-gallery/dispatch/claim \
  -H 'Content-Type: application/json' -d '{"channel": "lamp-case-2", "limit": 1, "lease_seconds": 300}' | jq -c '.tasks[0] | {task_id, lease_token}'
# => {"task_id":"bronze-gallery:g1:v1:lamp-case-2:0000","lease_token":"NEW_TOKEN"}   同一 task_id，令牌已换

# 用 OLD_TOKEN 确认 -> 409 token_conflict，状态不变
curl -sS -X POST http://localhost:8080/halls/bronze-gallery/dispatch/ack \
  -H 'Content-Type: application/json' \
  -d '{"task_id": "bronze-gallery:g1:v1:lamp-case-2:0000", "lease_token": "OLD_TOKEN"}'
# => {"ok": false, "code": "token_conflict", "message": "Lease token does not match the current token ...", "task_id": "..."}
```

### 13. 重新发布后：旧租约确认报告版本过期，新版本不补发

```bash
# 重新发布同一编排（版本 1 -> 2）
curl -sS -X PUT http://localhost:8080/halls/bronze-gallery \
  -H 'Content-Type: application/json' --data @examples/bronze-gallery.json >/dev/null

# 版本 1 的未确认任务失效：旧租约确认 -> 409 version_expired
curl -sS -X POST http://localhost:8080/halls/bronze-gallery/dispatch/ack \
  -H 'Content-Type: application/json' \
  -d '{"task_id": "bronze-gallery:g1:v1:lamp-case-2:0000", "lease_token": "NEW_TOKEN"}'
```

```json
{
  "ok": false,
  "code": "version_expired",
  "message": "Task 'bronze-gallery:g1:v1:lamp-case-2:0000' was issued for hall 'bronze-gallery' version 1, but the hall is now at version 2; the outstanding task is invalidated and was not confirmed.",
  "task_id": "bronze-gallery:g1:v1:lamp-case-2:0000",
  "task_version": 1,
  "current_version": 2
}
```

```bash
# 版本 2 不补发发布时间之前的指令（示例编排全部指令都早于重新发布时刻）
curl -sS -X POST http://localhost:8080/halls/bronze-gallery/dispatch/claim \
  -H 'Content-Type: application/json' -d '{"limit": 10, "lease_seconds": 60}' | jq -c '{version, count, tasks}'
# => {"version":2,"count":0,"tasks":[]}
```

删除展厅同理：旧租约确认返回 `version_expired`（`current_version: null`），领取返回 404 `hall_not_found`。批量发布（`POST /halls/batch-publish`）与单展厅 `PUT` 遵循完全相同的失效规则；校验失败（422）或版本冲突（409）的发布不触碰队列，在租任务仍可正常确认。

---

## 目录结构

```
app/
  models.py     # Pydantic 输入模型（含批量发布 BatchPublishIn、网关 DispatchClaimIn/DispatchAckIn）与时间归一化
  engine.py     # 纯函数：校验 + 时间线合成（抢占/恢复/跳过）
  simulator.py  # 任意时刻的通道状态模拟
  store.py      # 线程安全内存存储：单展厅 put 与批量原子 batch_commit（乐观版本检查）
  dispatch.py   # 网关任务派发：按通道顺序领取（租约制）、令牌确认、版本失效
  main.py       # FastAPI 路由（单展厅 + 批量发布 + 网关领取/确认）
tests/
  test_api.py       # 34 个端到端用例（单展厅 16 + 批量发布 18，含并发）
  test_dispatch.py  # 23 个网关派发用例（顺序/租约/确认/版本失效/并发）
examples/
  bronze-gallery.json
  batch-publish.json   # 批量发布请求示例（更新 + 新建）
Dockerfile
docker-compose.yml
requirements.txt
```
