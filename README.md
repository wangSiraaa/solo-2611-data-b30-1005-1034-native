# slabwater — 分块标高接缝、有开闸上限的蓄水核验与泡棉阴模制模

纯合成离散模型，仅用于可复现的规则核验，**不用于现场水利判断，也不是真实机加工模型**。

实现为 Python 3.11 独立库（仅标准库 + SQLite），分四层：

| 模块 | 职责 |
| --- | --- |
| `slabwater/catalog.py` | 独立、只追加的分块标高目录；批次校验、冻结切面、逐格来源 |
| `slabwater/solver.py` | 水位/蓄水计算；按出口集合搜索可行计划 |
| `slabwater/publisher.py` | 发布、幂等复读、拒绝留证、核计划、重开 |
| `slabwater/molding.py` | 基于**已发布计划**的泡棉阴模：刀口几何、最低安全刀柄高度、逐格可切、限程格、选程优化 |
| `slabwater/moldshop.py` | 定位贴实物库存与工单台账：试算不揭贴、确认才揭、后续单不得复用已揭贴 |

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

## 泡棉阴模（molding / moldshop）

制模只消费**已发布计划**里冻结的网格、出口与标高版本，不重新决定水利结果。
逐格目标厚度 = 统一毛坯高度 `M − 该格实际蓄水`（M 不小于最大水深）。

### 刀口与行程安全模型

- 每种刀给若干刀口：相对刀柄位置的网格偏移 + 刀口底部比刀柄基准**偏高**多少。
  刀柄高度 h 时，偏高 b 的刀口把所在格留到厚度 `h+b`（基准朝下，偏高越大切得越浅）。
- 一个获准行程是一串四邻相邻的刀柄位置，整趟 h 固定；每个刀口在每个位置都必须
  完整落在盘内，否则该行程在建模期即被拒绝。
- **同一格可被同一刀的多个刀口擦到**（含同偏移不同偏高）：取最低刀口
  `cut_to = min(h+b)`；多道行程覆盖同格同样取最小值，切深不叠加。
- 最低安全刀柄高度 `h_safe = max(目标[c] − 偏高)`（遍历所有触碰）；达到最大值的
  触碰所在格即**限制整道行程的格**。在 h_safe 下任何格都不会刨薄于目标（无过切）。
- 只在最低安全高度上枚举行程：更高的刀柄只会切得更浅，不可能更接近目标，因此
  不会进入最优解。这也意味着“某刀理论上够深”不能替代安全行程核算。

### 费用、刀架与定位贴

- 装刀费每种刀只计一次（同刀装一次可走多道），加工时费逐道计；刀架限制同时装刀数。
- 共用同一枚定位贴 id 的行程整批只能选一条；候选 ≤ 18 道、刀 ≤ 6 种（建模期强制）。
- 优化对最多 2^18 个子集做确定性 DFS 并按（费用、道数、id 元组）判优，交装哪些刀、
  走哪些道及 `review_grid` 逐格复核图（目标、最终厚度、各行程贡献、是否达标）。

### 实物库存（MoldShop）

- `quote`（普通试算/自选行程核价）**不揭贴**，只留非耗用记录；
  `confirm` 原子确认工单并把所用贴置为 `peeled`，已揭贴对后来的单不可再用，
  也可 `reserve_sticker`/`reserve=` 在不耗用的前提下给另一块模保住某贴。
- 确认按 request key + 规范化输入幂等；旧工单冻结完整 spec，
  `recompute_order` 在目录已切到别的源图后仍按冻结版本独立重算。

### 样例（端到端）

```bash
PYTHONPATH=. python3 -m slabwater.molddemo
```

- `materials/closed1x3`：闭口 C=2 三格，复刻题首 3/5/6/6 四案与两种实物落账；
- `materials/multiedge1x3`：同一格被同刀两个不同偏高刀口擦过，最低刀口决定切深；
- `materials/772/mold.json`：建在真实发布单 A2（蓄水 `[1,0,2,1,3]`，M=4，
  目标 `[3,4,2,3,1]`）上；整行宽刨 W-FULL 被目标 4 的格逼到刀柄高度 4 而走不完，
  台阶宽刀 W-23 一次恰切两格，混合刀工单 12 低于全点刀 16，确认揭 S-W 后后单只能退回点刀。
