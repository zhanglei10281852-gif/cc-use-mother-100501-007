# 六网项目联动排程后端

面向"七万亿元级基础设施多网同时开工"场景的跨项目联动排程后端：建设单位登记里程碑、前置条件、共享资源、监管许可与交付承诺；省级专班基于**已批准的联合基线**计算跨网关键链，在计划变更、里程碑延误或豁免到期时**只重算受影响范围**，并让并发提交经过责任方确认后才能改变基线，月度结论永久可追溯。

仅依赖 Python 3.11 标准库，无需数据库或外部服务（持久化使用事件溯源 JSON 日志 + 文件锁）。

## 能力总览

| 需求 | 实现 |
| --- | --- |
| 登记里程碑/前置/共享资源/许可/承诺 | 不可变领域模型 `model.py`，稳定内容指纹 |
| 硬阻断 / 可替代 / 带期限临时豁免 | `DependencyKind` 三类；豁免在 `as_of` 晚于到期日时自动恢复硬阻断 |
| 基于已批准版本计算关键链 | 前向 CPM（FS+lag、"或"前置组、许可门、资源排队）+ 后向总时差 |
| 循环依赖识别 | Tarjan 强连通：硬循环（阻断）、豁免到期潜在环、可替代弱环 |
| 同一资源超额占用 | 容量日历超载事件 + 确定性优先级串行化平衡 |
| 变更/延误/豁免到期只重算受影响范围 | 触发点下游闭包 + 资源级联不动点，边界节点冻结（`recompute_incremental`） |
| 并发更新须责任方确认后改基线 | 提议 → 责任方确认 → 专班批准工作流；修订版+指纹乐观并发检测，覆盖者置 `SUPERSEDED` |
| 月度结论可追溯 | 不可变发布（月份唯一）、内容指纹、完整事件链审计 |
| 表面正常却被上游拖延 | 局部视图（仅网内依赖/本网资源）vs 全链视图差异分析 |
| 不同解法牺牲哪些承诺 | 方案评估（赶工/资源优先序/豁免展期/许可假定获批），按牺牲承诺数与加权延期排序 |
| 最终方案的完整批准过程 | `/api/audit/trail`：每一修订版对应的提议、确认、批准人与时间 |

## 目录结构

```
src/program_scheduling/
  contracts.py   基础领域契约（稳定标识/指纹/冲突检测，初始提交保留）
  model.py       项目/里程碑/三类依赖/资源/预约/许可/承诺/提议等不可变模型
  engine.py      关键链、环检测、资源平衡、增量重算、隐藏延误、方案评估
  store.py       事件溯源存储、提议协同工作流、月度发布、乐观并发
  service.py     应用服务（引用完整性校验、责任方推断、调度缓存、研判、月报）
  api.py         REST API（http.server，含 ThreadingHTTPServer）
tests/           53 个单元/集成/HTTP 端到端/并发测试
run_cli.py       题述三网联动场景的端到端演示
```

## 运行环境

- Python 3.11+（使用了标准库，无第三方依赖）
- Linux / macOS / Windows（无 `fcntl` 时文件锁退化为进程内锁）

## 运行测试与检查

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m compileall -q src tests run_cli.py
```

## 端到端演示

```bash
python3 run_cli.py
```

演示以"算力通道等变电站扩容、变电站与地下管廊共用滨河路施工窗口、监管许可以及临时豁免"为原型，依次输出：

1. 已批准基线上的关键链排程、道路窗口超载与平衡后排队；
2. 各项目"局部按时、全链违约"的隐藏延误与具体跨网原因；
3. 维持现状 / 压缩变电站工期 / 道路让位三种方案牺牲的承诺与加权代价排序；
4. 豁免到期后仅重算 3 个受影响节点的结果；
5. 不可变月度结论发布与批准链回溯。

## 启动 REST 服务

```bash
PYTHONPATH=src python3 -m program_scheduling.api --host 0.0.0.0 --port 8080 --data-dir ./data/sixnet
```

主要接口（均为 JSON）：

| 方法 & 路径 | 说明 |
| --- | --- |
| `POST /api/baseline/initial` | 建立初始联合基线 |
| `GET  /api/baseline` | 当前修订版、实体指纹与修订历史 |
| `GET  /api/entities/{kind}` | 查询七类实体 |
| `POST /api/proposals` | 提交变更提议（自动推断受影响项目与责任方，支持幂等键） |
| `POST /api/proposals/{id}/confirmations` | 责任方确认 |
| `POST /api/proposals/{id}/approve` / `reject` | 专班批准并入基线 / 驳回 |
| `GET  /api/schedule?as_of=YYYY-MM-DD&incremental=1` | 关键链排程（默认走增量缓存） |
| `POST /api/schedule/recompute` | 显式指定触发点的定向重算 |
| `GET  /api/analysis/hidden-delays` | 表面正常却被上游拖延的项目 |
| `POST /api/analysis/scenarios` | 多方案牺牲承诺对比排序 |
| `POST /api/monthly` / `GET /api/monthly/{YYYY-MM}` | 发布（不可变）/ 查阅月度结论 |
| `GET  /api/audit/trail` | 全部修订版的完整批准过程 |

### 典型协同时序

```jsonc
// 1) 建设单位提交（required_parties 由系统按受影响项目自动推断）
POST /api/proposals
{"proposal_id":"PR-2026-001","submitted_by":"电力公司","changes":[...]}

// 2) 每位责任方分别确认
POST /api/proposals/PR-2026-001/confirmations
{"party":"电力责任人","actor_id":"leader-7","comment":"现场核实无误"}

// 3) 全部确认后专班批准；若基线中同一实体已被并发提议改变，返回 409
//    且本提议被置为 SUPERSEDED，需基于新版本重新提交
POST /api/proposals/PR-2026-001/approve
{"approver":"省级专班","comment":"并入联合基线修订版 2"}
```

## 调度语义摘要

- **硬阻断（HARD）**：完成到开始（FS）加 `lag_days`；硬边上的环直接阻断并沿硬边传播。
- **可替代（ALTERNATIVE）**：同 `alternative_group` 的前置为"或"，取最早就绪者；其构成的环只作弱环提示，不阻断。
- **限期豁免（EXEMPTION）**：`as_of <= exempt_until` 时临时放行并在潜在环中预警；到期后自动等同硬阻断，触发只含相关闭包的增量重算。
- **监管许可**：`PENDING` 以计划日期作为里程碑门控（超期未批产生预警），`GRANTED` 以获批日放行，`REJECTED` 为硬阻断。
- **共享资源**：先输出原始容量日历中的全部超载事件，再按（意图开始日, 优先级, 编号）确定性串行化；排队位移反压为里程碑资源门，门控单调取最大值保证收敛。
- **延误登记**：未完成里程碑若按最早开始推算的完工日早于 `as_of`，从 `as_of` 起按剩余工期重新预测。
- **增量重算**：对比两个基线快照自动派生触发点（实体变更、豁免翻转、许可/资源变更），沿依赖边与共用资源做不动点扩张求影响域，域外节点与未触及资源结论原样继承。

## 持久化与并发

- 所有状态变化先 `fsync` 追加到 `events.jsonl`，再重建内存聚合；服务重启后状态一致。
- 写操作在 `flock` + 线程锁内完成；提议携带基线修订版与受影响实体指纹快照，批准时检测并发覆盖。
- 月度结论与基线修订版均不可变、带指纹，可长期审计与复核。
