# slabwater — 分块标高接缝与有开闸上限的蓄水核验

纯合成离散模型，仅用于可复现的规则核验，**不用于现场水利判断**。

实现为 Python 3.11 独立库（仅标准库 + SQLite），分三层：

| 模块 | 职责 |
| --- | --- |
| `slabwater/catalog.py` | 独立、只追加的分块标高目录；批次校验、冻结切面、逐格来源 |
| `slabwater/solver.py` | 水位/蓄水计算；按出口集合搜索可行计划 |
| `slabwater/publisher.py` | 发布、幂等复读、拒绝留证、核计划、重开 |

## 目录（Catalog）

固定 `H×W` 格面，每块给出 `tile_id`、`revision`、原点 `(row,col)`、整数矩形与高度。一次批次：

- 可同时给出多块，**按 `tile_id` 替换**；批次未提及的块沿用上一版。
- 批次整体校验，全部通过才追加一个新版本；失败则**不落任何版本**，旧产品不变。
- 校验项：矩形越界、`tile_id` 重复、同一 `(tile_id,revision)` 内容被改写（immutable）、
  切面**未完整覆盖**（`coverage_gap`）、**重合格高度不一致**（`overlap_mismatch`）、
  格高超过上限 `C`（`height_above_ceiling`）。
- 每版冻结 `heights`、`elevation_hash` 与逐格 `sources`（来自哪块、块的修订、行列局部坐标）。

历史版本可随时 `get_slice(version)` 读取，发布端据此独立取标高，互不影响。

## 水位模型（Solver）

四邻移动，整数高度。对已打开的出口集合：

1. 一格到某打开出口的**排水门槛** = 路线经过的所有格高与出口门槛 `sill` 的最大值。
2. 该格水位 = `min(C, 所有打开出口门槛的最小值)`；无任何出口可达时为 `C`。
3. 蓄水 = 水位 − 本格高（逐格一致计算）。

这不是最短路求和，而是**最小化最大瓶颈**（minimax）的 Dijkstra：扩展边时取
`max(当前瓶颈, 邻格高)`。因此**不能直接用最低出口门槛**——路径上的高堤仍会抬升水位
（见测试 `test_lowest_sill_alone_is_not_enough`）。

### 两种搜索策略

- `recompute`：对每个候选出口集合跑一次多源 minimax Dijkstra。直接、易证正确。
- `reuse`：每个出口只算一次逐格瓶颈图，集合内逐格取**最小**组合。候选集合多时更省。
  瓶颈图只对**某一冻结切面**有效。

`BottleneckCache` 以 `elevation_hash` 为键：切面一旦改变（新版本、标高修订），
下次查询立即**清空失效**并计数 `invalidations`。在 12×12、8 出口压力样例中
（最多开 8 个，共 256 个集合）：

| 策略 | Dijkstra 次数 | 耗时 | 结果 |
| --- | --- | --- | --- |
| reuse | 8（每出口一次） | 0.028s | 与 recompute 完全一致 |
| recompute | 256 | 0.119s | — |

选择建议：候选集合少、或只需核一个既定集合 → `recompute` 更直接；候选集合多且同一
切面反复查询 → `reuse`。任何标高修订后不要跨切面复用瓶颈图，交给缓存自动失效。

搜索按“集合大小升序、再按出口 id”确定性枚举，因此同输入必然得到同一产品（无需最小开闸数）。
`stop_on_first=True` 命中即返回；`False` 则遍历全部并在可行集合中取总蓄水最小者，同时保留全部尝试。

## 发布（Publisher）

请求需给候选出口、`opens`（最多开启数，未要求必须开满）、以及 `total_max` 与 `cell_max`
（若干指定格蓄水上限）。消费端只读**已冻结**的 `source_revision` 切面。

- **成功**：写正式 `plans` 行，产品含开启出口、逐格水位/蓄水、总量、可复查的
  `elevation_source`（版本、`elevation_hash`、块清单）与 `artifact_hash`。
- **拒绝**：不写正式计划；保存真实尝试的**穷尽证据**：每个候选集合的开口、总量、指定格蓄水
  与违反的约束（`total` / `cell`），以及 `exhaustive=true`。可直接下载核对。
- **错误**（未知版本、出口越界/不在边界、请求非法）：记错误结果，同样不写正式计划。
- **幂等**：同一申请 `id` + 同一输入 → 原样复读首次结果（成功返回同一 `plan_id` 与工件；
  失败返回同一证据）。同一 `id` 换输入 → `idempotency_conflict`，绝不静默覆盖。
- **版本隔离**：新标高版本不改写旧 `plans` 行；`get_plan`/`verify`/`reopen` 均按产品内
  冻结的版本与哈希核验。

### 读标高 / 核计划 / 发布 / 重开入口

```python
from slabwater import Catalog, Publisher

catalog = Catalog("catalog.db", height=1, width=5, ceiling=6)
publisher = Publisher("plans.db", catalog)

catalog.apply_batch([...tiles...])          # 追加切面版本
publisher.read_elevation(version=1)         # 读标高（含逐格来源）
publisher.publish(request, default_outlets=[...])  # 实际发布
publisher.verify(plan_id)                   # 核计划（重算并重搜索）
publisher.reopen(plan_id)                   # 重开已存工件
```

命令行：

```bash
python3 -m slabwater run materials/772/input.json --db-dir /tmp/run772
python3 -m slabwater verify /tmp/run772 <plan_id>
python3 -m slabwater reopen /tmp/run772 <plan_id>
```

## 样例核对（materials/772）

- 初版 `[0,4,1,4,0]`，双口全开：水位 `[1,4,4,4,3]`，蓄水 `[1,0,3,0,3]`，总 7，中格 3。
  A1（总量≤7、中格≤2）必拒：没有任何集合同时满足，证据穷尽列出（含双开的 `cell` 违例）。
- 降右侧堤后 `[0,4,1,2,0]`，双口全开：水位 `[1,4,3,3,3]`，蓄水 `[1,0,2,1,3]`，总 7、中格 2。
  A2（≤7、中格≤2）成功，开启 `L,R`。
- A3 只允许开 1 口：`L` 总 10、`R` 总 10，均超总量 7（`R` 虽满足中格≤2），故拒绝。
- `invalid_correction` 使重合格 `(0,2)` 出现 1 与 2，冲突 → 整版拒绝，版本仍为 `[1,2]`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖：切面/接缝/覆盖/越界/不可变修订/原子批次/重开；水位规则与“最低门槛不充分”；
reuse 与 recompute 一致；修订使缓存失效；A1/A2/A3；幂等复读与冲突；版本隔离；
核计划能发现篡改；12×12、8 出口压力与穷尽拒绝证据。

## 展柜天平（tray / balance）

在 Catalog/Publisher 之上为桌面沙盘加的展柜配平层，纯模型、纯标准库
（`fractions.Fraction` 精确有理数），无现场设施。

| 模块 | 职责 |
| --- | --- |
| `slabwater/tray.py` | 浅盘标定：每格冻结地面 `h`（Catalog 切面）与水位上限 `l`（Publisher 发布单的逐格水位）；共同刻线 `z` 下 `water=max(0,min(z,l)-h)`、力矩=水量×有向力臂；产出分段精确的水量—力矩关系、满盘容量、格来源（含 tile 修订）与水量反查 |
| `slabwater/balance.py` | 最多三盘配平：枚举砝码子集（至多 3 块、可挂可不挂）×每盘分段，把总量守恒与目标力矩写成线性等式，在盒约束多面体上做精确顶点枚举（高斯-若尔当），最小化“移水费（每份水只计一次）+挂费” |

要点：

- **按格冻结、重发不改旧盘**：`Pan` 绑定 `plan_id`/`catalog_version`/`elevation_hash`；
  新切面或新申请号只产生新盘，旧盘对象的 `h/l` 不变。一盘的不同格还可引用不同发布单
  （`Pan.from_cell_plans`），但必须同一切面。
- **平台与反查**：刻线走过干格/满格时出现 `dW=0` 平台段；`lookup_water` 对同一水量
  返回全部合法刻线区间 `[z_lo, z_hi]`，只问最低地面到最高水位之间。
- **每盘至多 8 个转折**：超出直接拒绝；力臂可负使力矩非单调，跨高台会突然多一格
  吃水——分段表逐段精确处理，不假设“只剩一次线性插值”。
- **只固定总水量决定不了哪边重**：同一总量的不同分配力矩不同；配平同时给出砝码
  选择与各盘刻线，输出含两侧（正/负）力矩与含砝码净力矩验算。

端到端复跑（真实切面与发布单，含题面三个费用场景 2 / 3 / 3/2、同盘先后进水的
均分红牌反例、干台/满台平台、不可行场景）：

```bash
python3 -m examples.display_balance
```
