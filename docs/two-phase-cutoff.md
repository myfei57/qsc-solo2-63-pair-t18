# 两段截载设计（软截载 → 收尾窗口 → 硬截载）

## 背景

旧的截载是一刀切：到点航班直接翻状态，值机口还排着队的旅客全部被拒，
意见很大。问题在于"停止收运"这个动作没有区分两类行李：

- **还没发指令的**（旅客还在排队，尚未收运）——这部分才是截载真正要拦的；
- **已经收运在途的**（行李已上皮带）——这部分必须送到，否则就是丢行李。

因此把截载拆成两段，中间留出一个可观察、可回退的收尾窗口。

## 状态机

```
OPEN / BOARDING ──soft_cutoff──▶ CUTOFF ──hard_cutoff──▶ CLOSED
                     ▲               │
                     └──── reopen ───┘
```

| 状态 | 语义 | 新收运 | 在途行李 |
|---|---|---|---|
| `OPEN`/`BOARDING` | 正常收运 | 允许 | 正常分拣 |
| `CUTOFF`（软截载） | 停发新指令，收尾窗口开启 | **拒绝** | 继续按原路由分拣 |
| `CLOSED`（硬截载） | 终态 | **拒绝** | 不允许存在（前置条件） |

要点：

- **软截载不是幂等的**：已在 `CUTOFF` 的航班再次 soft_cutoff 报
  `OrderingError`，防止有人用重复调用覆盖基线快照。
- **硬截载有前置条件**：必须先从软截载进入（`CUTOFF`），且在途队列为空。
  在途不为空时硬截载被拒，提示先收尾或强制排空（`drain_in_transit`，
  滞留行李转 DIVERTED）。
- **硬截载是终态**：`CLOSED` 之后 `reopen` 同样报 `OrderingError`，
  硬截载之后不得再收运这一约束由状态机保证，不靠自觉。
- 旧的一刀切路径（`close_flight`，先排空再关闭）保留作应急通道，
  但正常截载一律走两段流程。

## 软截载：只收不再发新指令的行李

`soft_cutoff` 在一个**日志会话**里同时提交两件事（要么都落盘，要么都不落）：

1. `flight.put`：航班状态翻为 `CUTOFF`——值机入口 `require_open` 随即拒绝
   一切新收运（不再发新指令）；
2. `cutoff.begin`：`CutoffMark` 基线快照，记录此刻的
   `in_transit`（在途件数）与 `accepted`（累计收运件数）。

基线与状态翻转同事务提交，是"两段之间状态可恢复"的关键：重启后重放日志，
要么一起重现，要么一起不出现，绝不会出现"航班已截载但没有基线"的中间态。

## 两段之间：看住在途（三方印证）

`GET /api/flights/{id}/cutoff-status` 把三个独立提交的来源放在一起对账，
外加分拣日志作第四方见证：

| 来源 | 内容 |
|---|---|
| 航班状态（flight book） | 当前状态 + `CutoffMark` 基线 |
| 收运流水（bag ledger） | 该航班全部已提交行李记录，按状态分桶计数 |
| 在途队列（injector） | `INJECTED` 状态行李的实时队列（含 bag_id 明细） |
| 分拣日志（sorter log） | 每条分拣记录，与流水逐件对应 |

一致性检查（`checks`，全部通过才算 `consistent`）：

- `queue_matches_ledger`：在途队列长度 == 流水中 `INJECTED` 计数；
- `sort_log_matches_ledger`：分拣日志条数 == 流水中被分拣过的行李数
  （不多分拣一件，也不少记一件）；
- `tail_not_growing`：窗口期内在途数只减不增——**一旦增长，说明软截载后
  仍有新行李混入，这是必须立刻报警的违例**。

`tail_empty` 单独报告（不计入 `consistent`）：窗口期内在途非空是健康状态，
它只是硬截载的前置条件。

## 审计痕迹与逐条复查

三个领域事件各产生一条哈希链审计条目（stage = `audit`）：

| 事件 | 审计 kind | 携带明细 |
|---|---|---|
| `flight.cutoff_soft` | `flight_cutoff_soft` | `began_at`、`in_transit`、`accepted` |
| `flight.reopened` | `flight_reopened` | 回退时刻的 `in_transit` |
| `flight.cutoff_hard` | `flight_cutoff_hard` | `closed_at` |

`GET /api/flights/{id}/cutoff-review` 按提交顺序返回该航班的全部截载条目，
每条都带当时的在途/收运数字，审计员可以逐条复查整个截载过程；
`GET /api/audit/verify` 校验哈希链完整，条目不可篡改、不可删除。

## 收尾槽决策：继续按原滑槽收

**选定：软截载期间在途行李继续走该航班的原滑槽，不设专门收尾槽。**

理由：软截载已经切断新行李来源，原滑槽在窗口期内本来就只会有收尾行李，
"物理隔离"买不到任何东西。而收尾槽的代价是实打实的：

1. **多占一条滑槽资源**，占用时长 = 整个收尾窗口；
2. **滑槽指派 generation 被迫 +1**，所有缓存了旧路由的在途指令立刻失效
   （stale-reference 拒收），必须在航班最紧张的几分钟内重新确认路由；
3. **已分拣的行李留在旧槽**，地服在重叠期要同时扫两个槽，反而拖慢收尾；
4. 换槽本身是截载关键路径上**多出来的一次可失败的受审计操作**。

结论：原滑槽零路由扰动、零额外资源，收尾效率最高。若未来某航站楼的
物理布局确实需要收尾槽，以上四条就是引入它时必须接受并重新评估的成本。

## 可恢复性与不重复收运

- **可恢复**：`CutoffMark` 与状态翻转在同一日志会话提交；重启后重放日志
  即可重建收尾窗口（有测试覆盖：日志重放与快照恢复两条路径）。
- **退回开放**：`reopen` 只允许 `CUTOFF → OPEN`。它只翻航班状态、注销
  基线，**不触碰任何行李记录、映射和分拣日志**，因此不存在"重新收运
  一遍"的可能：
  - 行李身份来自已提交的条码序列（单调递增，不回退）；
  - 分拣器对每件行李只认一次 dispatch（`was_executed`），重放被拒；
  - 退回后再次软截载会取**新基线**，旧基线已注销，不会重复计数。
- **硬截载后不得再收运**：`CLOSED` 不是开放状态，值机入口拒绝；
  `soft_cutoff`/`reopen` 对 `CLOSED` 同样拒绝。终态由状态机锁死。

## 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/flights/{id}/cutoff` | 软截载：停发新指令 + 基线快照 |
| POST | `/api/flights/{id}/open` | 退回开放（仅软截载期间可用） |
| POST | `/api/flights/{id}/hard-cutoff` | 硬截载（前置：在途为空） |
| GET | `/api/flights/{id}/cutoff-status` | 三方印证对账视图 |
| GET | `/api/flights/{id}/cutoff-review` | 截载审计条目，逐条复查 |

## 实现位置

- `bagsort/flight/cutoff.py` — `CutoffManager`：两段切换、基线、对账；
- `bagsort/model/entities.py` — `CutoffMark` 基线实体；
- `bagsort/model/state.py` — `cutoffs` 状态与 `cutoff.begin/reopen/finish`
  三个日志命令的归约；
- `bagsort/app.py` — 事件发布与审计映射；
- `bagsort/console/{routes,handlers}.py` — 五个控制台端点；
- `tests/test_cutoff.py` — 16 个用例覆盖状态机、对账、回退、恢复与审计。
