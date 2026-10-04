# 港区冷藏箱供电台账

纯 Python 标准库实现的港区冷藏箱供电管理原型，SQLite 持久化，HTTP 接口由 `http.server` 提供，替代“白板记回路、跳闸漏箱”的做法。

## 台账实体与规则映射

| 白板痛点 | 台账做法 | 关键列/事件 |
| --- | --- | --- |
| 冷藏箱、回路、跳闸、装船各记各的 | 统一 Ledger：冷藏箱/回路/航次/接电/跳闸/批次/排队/冲突候选 | `GET /api/ledger` |
| 接电前核对容量和温控 | 候选回路必须 `active`、温控区间覆盖 `设定±容差`、剩余容量 ≥ 需求 | `connected.basis` 记录核对结论 |
| 容量不够 | FIFO 队列 + 缺口 kW（取温控满足回路中最大剩余与需求之差；无温控匹配则全额缺口） | `queues.gap_kw`，审计 `queued` |
| 船期/回路状态一变就漏箱 | 跳闸/检修/改期：**未装船**安排作废（连接 `voided`）并自动重排；**已装船**连接转 `loaded_frozen`，状态、容量释放但依据保留 | 审计 `invalidated` / `basis_kept` / `rescheduled` |
| 两人同时提交接电 | `BEGIN IMMEDIATE` 串行化，先到先得；后来者的提交落 `conflict_candidates`（事务提交后再返回 409，候选不回滚） | `conflict_candidates`，审计 `conflict_candidate` |
| 接电失败恢复 | 批量接电前统一核对，任一箱不满足则**整批不产生接电记录**；`batch_fail` 后从**最近完整批次快照**恢复：原回路可用则同回路，否则改配；仍无回路的箱转 `pending_recovery`（队列原因 `recovery`，只由批次恢复消费） | 批次 `complete/failed` 快照、审计 `restored` / `pending_recovery` / `recovery_applied` |
| 页面与审计追来源 | 每条接电/跳闸/重排/恢复事件都带 `source`（如 `connect#3`、`trip#1`、`reschedule:circuit#2_restored`、`recovery:batch#1`），页面点击任意行看时间线 | `GET /api/{实体}/{id}/audit`、`GET /api/events` |

冷藏箱状态机：`waiting → connected → loaded`；任意未装船阶段可因容量不足入 `queued`、跳闸入 `pending_circuit`、批次恢复缺回路入 `pending_recovery`；已装船冻结，不再参与重排。

## 模块结构

- `app.py`：参数解析、依赖组装和服务启动。
- `src/domain.py`：错误类型、Actor、基础输入校验。
- `src/rules.py`：温控覆盖、容量/候选回路选择（best-fit）、缺口计算、角色矩阵、状态守卫。
- `src/repository.py`：SQLite 多表台账、`BEGIN IMMEDIATE` 写事务、审计写入。
- `src/service.py`：接电核对、跳闸/检修/改期失效重排、装船冻结、队列泵动、批次恢复、冲突候选处理。
- `src/http_api.py`：HTTP 路由（复数资源 + 动作子路径 + 审计时间线）。
- `src/audit.py`：时间线只读封装。
- `static/index.html` / `static/app.js`：台账演示页（登记、接电、跳闸/恢复、改期、批次、冲突处理，点行看审计）。
- `tests/`：规则计算、完整流程与失败恢复测试（17 个用例）。

## 启动

```bash
python3 app.py --db ./reefer-ledger.db --port 8321
```

## 身份与角色

除 `/health` 与 `/` 外所有接口要求请求头 `X-User-Id`、`X-Role`。

- `yard_planner`：建档（冷藏箱/回路/船期）。
- `electrician`：接电、跳闸登记/恢复、批次。
- `vessel_clerk`：船期改期、装船放行。
- `admin`：全部操作。所有已知角色均可只读查询台账。

## 主要接口

建档：

- `POST /api/circuits`：`{data:{circuit_code,capacity_kw,temp_min_c,temp_max_c,bay}}`
- `POST /api/voyages`：`{data:{vessel,voyage_no,sail_hour}}`
- `POST /api/reefers`：`{data:{reefer_no,required_kw,temp_setpoint_c,temp_tolerance_c,voyage_id?}}`

动作：

- `POST /api/reefers/{id}/connect`：`{data:{preferred_circuit_id?}}`，容量/温控不足自动排队写缺口；重复提交返回 409 并生成冲突候选。
- `POST /api/reefers/{id}/load`：装船放行（必须已接电），释放岸电容量并冻结原依据。
- `POST /api/circuits/{id}/trip` / `/recover` / `/maintenance`：跳闸登记（未装箱失效、装船箱保留）、恢复投用（触发重排）、检修。
- `POST /api/voyages/{id}/revise`：`{data:{sail_hour}}`，未装船安排立即失效重排，已装船保留。
- `POST /api/batches/connect`：`{data:{reefer_ids:[...], preferred:{"箱id":回路id}}}`，整批核对通过才落接电记录。
- `POST /api/batches/fail`：登记失败批次（不落接电）并自动按最近完整批次恢复。
- `POST /api/batches/recover`：手动触发“从最近完整批次恢复”，缺回路转待补。
- `POST /api/conflicts/{id}/resolve`：`{data:{decision:"discard"|"queue"}}`。
- `POST /api/queues/pump`：手动重排队列（`recovery` 待补项除外）。

查询：

- `GET /api/ledger`：一次返回冷藏箱（含当前接电/排队）、回路（占用/剩余）、航次、跳闸、队列、批次、冲突候选。
- `GET /api/reefers?state=`、`/api/circuits`、`/api/voyages`、`/api/trips`、`/api/queues`、`/api/batches`、`/api/conflicts`。
- `GET /api/reefers/{id}/audit`、`/api/circuits/{id}/audit`、`/api/voyages/{id}/audit`、`/api/batches/{id}/audit`。
- `GET /api/events?entity_type=&limit=`：全局审计流，`details.source` 可追到接电、跳闸和重排来源。
- `GET /api/stats`、`GET /health`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖：温控覆盖与 best-fit 选路、容量缺口、跳闸失效/装船保留、改期、并发先到先得与冲突候选、批次失败回滚/最近完整批次同回路恢复/无批次待补、无接电不得装船、权限与重复建档。
