# 港区冷藏箱供电回路台账

纯 Python 标准库实现的港区冷藏箱接电管理台账，SQLite 持久化，`http.server` 提供接口，单文件页面做台账展示与操作。

## 解决的问题

港区原先只用白板记录冷藏箱的供电回路，回路跳闸后经常漏箱。本系统把**冷藏箱、供电回路、跳闸记录、装船放行**接成一本台账，核心规则：

1. **接电前核对容量和温控**：实测温度与设定温度偏差超过 3°C 直接拒绝；容量够才接电，容量不够进入排队并写入容量缺口（gap_kw）。
2. **船期或回路状态一变立即失效重排**：未装船的接电安排标记为 `invalid`（保留为依据）并重新排队，容量可用时按先到先得自动补电；**已装船安排定格为 `loaded`，保留原依据不动**，并释放岸电容量。
3. **并发接电先到先得**：接电申请在 `BEGIN IMMEDIATE` 事务中裁决，两人同时提交同一冷藏箱，只认先到的一份；后来者不产生安排，只留 `conflict_candidate` 申请记录并返回 409。申请带 `request_id` 幂等键，重放返回原裁决。
4. **跳闸后从最近完整批次恢复**：跳闸只记录失电、不立即偷接别的回路；恢复时取跳闸时间点之前最近的“完整批次”，按批次成员逐个恢复；容量不够的冷藏箱按 `pending_supply`（待补）处理并记缺口，新容量到达后自动补电。
5. **全链路可溯源**：接电安排带 `basis`（接电/排队/重排/批次恢复依据），每条审计事件带 `sources` 来源链，页面与 `/api/audit` 可逐级追到接电申请、跳闸单、批次和重排来源。

## 状态模型

- 冷藏箱：`registered` → `loaded`
- 回路：`normal` / `maintenance` / `tripped`
- 接电安排（每箱至多一条活跃）：`queued` / `connected` / `pending_supply` 为活跃；`invalid`、`superseded`、`loaded` 为历史依据
- 跳闸：`open` → `recovered`（记录恢复所用批次）
- 批次：`open` → `complete`（成员全部接电后才能封存为完整批次）

## 模块结构

- `app.py`：参数解析、依赖组装、服务启动
- `src/domain.py`：错误类型、Actor、输入校验
- `src/rules.py`：温控容差、容量核对、紧凑选回路、缺口计算（纯函数）
- `src/repository.py`：SQLite 表结构、`BEGIN IMMEDIATE` 事务与查询
- `src/service.py`：用例编排（接电裁决、失效重排、装船放行、跳闸恢复、审计来源链）
- `src/audit.py`：审计事件与来源（sources）写入
- `src/http_api.py`：HTTP 路由与统一错误响应（409 带冲突候选详情）
- `static/index.html`：台账页面（总览、建档、接电、放行、跳闸/恢复、冲突候选、审计溯源）
- `tests/`：规则、完整流程、失败边界、并发、HTTP 端到端测试

## 启动

```bash
python3 app.py --db ./reefer-ledger.db --port 8321
```

## 接口

除 `/health` 和 `/` 外，请求需带 `X-User-Id`、`X-Role`（角色：`yard_planner` 堆场调度、`electrician` 电气员、`admin`）。

| 方法 | 路径 | 角色 | 说明 |
|---|---|---|---|
| GET | `/health` | - | 健康检查 |
| GET | `/` | - | 台账页面 |
| POST | `/api/reefers` | planner | 登记冷藏箱（箱号、航次、需求功率、设定温度） |
| POST | `/api/circuits` | planner | 登记供电回路（容量、位置、初始状态） |
| POST | `/api/voyages` | planner | 建船期（ETD 必须 ISO-8601） |
| POST | `/api/batches` | both | 开接电批次（不带批次的申请自动归入 AUTO） |
| POST | `/api/batches/{no}/complete` | both | 封存完整批次（成员须全部接电） |
| POST | `/api/connect-requests` | electrician | 提交接电申请 `{reefer_code,request_id,actual_temp_c,preferred_circuit?,batch_no?}` |
| POST | `/api/gate-release` | planner | 装船放行 `{voyage_no,reefer_codes[]}`（必须已接电） |
| POST | `/api/voyages/{no}/reschedule` | planner | 船期变更，未装船安排立即失效重排 |
| POST | `/api/circuits/{code}/state` | both | 回路切 normal/maintenance |
| POST | `/api/trips` | electrician | 登记跳闸，失电箱失效入队 |
| POST | `/api/trips/{id}/recover` | electrician | 从最近完整批次恢复，缺回路按待补 |
| GET | `/api/reefers` `/api/circuits` `/api/voyages` `/api/batches` `/api/trips` | both | 台账列表（回路带实时已用/剩余容量） |
| GET | `/api/assignments?active=1` | both | 接电安排（含 basis 依据） |
| GET | `/api/candidates` | both | 接电申请裁决记录（含冲突候选） |
| GET | `/api/audit?entity_type=&entity_id=` | both | 审计时间线（含 sources 来源链） |
| GET | `/api/stats` | both | 台账统计 |

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖：温控/容量规则、容量缺口排队与自动补电、船期与回路失效重排（已装船不动）、并发双提交先到先得+冲突候选、幂等重放、跳闸后批次恢复与缺回路待补、权限与状态校验、HTTP 端到端。
