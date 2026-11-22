# 多采样率剪辑时间轴与真实混音档案（audiomix）

纯 Python 3.11 实现（NumPy/SciPy/标准 `wave`），无视频、无音频设备、无网页。
三个独立服务：

1. **可独立编辑的音频时间轴服务**（`audiomix/timeline.py`）：插入/移动/修改/删除/
   区间查询/发布，clip 身份独立于素材，有理数时间，不渲染即可点查询。
2. **实际混音数据仓**（`audiomix/archive.py`）：只消费不可变发布产品与实际素材字节，
   固定 16kHz 输出，按固定样本块存档，内容寻址复用，支持范围读取与逐块追溯；
   不读取编辑器私有操作日志，也接受外部合法时间轴产品。
3. **双盲听审制作与排期**（`audiomix/blindstim.py` / `blindschedule.py` /
   `blindtrial.py` / `blindcli.py`）：纯标准库实现，不依赖 numpy；把等长 render
   区间与批准增益档封成无 A/B 标签的匿名实包，再按听者空闲表/耳机容量排出两序
   相反的两场，并由独立校验器与封存追溯表闭环复核。

## 目录

```
audiomix/
  rationals.py   精确有理数（Fraction）表示与边界整数运算
  assets.py      PCM WAV 生成/读取 + 素材登记仓（asset_id=正文 SHA-256，绑定摘要/样本数）
  timeline.py    时间轴编辑器（整笔校验、点查询、不可变发布产品）
  engine.py      混音引擎：逐样本参考解释器 + 等价的 NumPy 分块向量化渲染
  archive.py     混音数据仓：内容寻址块存储 + SQLite 依赖索引 + 增量渲染 + 读取/追溯
  blindstim.py   盲听刺激：整数增益配平/越界整组退回/全同拒绝 + 匿名实包与封存追溯表
  blindschedule.py 两序排期：洗耳空档、回避规则、耳机容量、跨人额度，回溯+独立校验器
  blindtrial.py  统筹：案例 -> 刺激 -> 排期 -> 逐人匿名播放单 -> 封存复核
  blindcli.py    盲听命令行（JSON 案例输入）
  cli.py         命令行入口
tests/           手算案例、数据仓案例、边界语义测试、盲听 15 个手算案例
demo/            30 素材/100 clip/120s 规模案例、盲听案例 JSON 与两套 CLI 走查
run_all_checks.sh 一键全量验证
```

## 核心语义（均为精确有理数，杜绝浮点边界误差）

- 素材：单声道有符号 16bit PCM，采样率仅 8000/16000/48000；
  `asset_id = SHA-256(PCM 正文)`，同字节内容永远同一身份，绑定摘要与样本数。
- clip：`clip_id` 与素材独立；同一素材可被任意多 clip 反复引用；多 clip 允许重叠。
  时长 `(b-a)/(源采样率×速度)`，参数全部为正/非负有理数。
- 编辑整笔拒绝：未知素材、源区间 `[a,b)` 越界、淡化之和超过 clip 时长等，
  单次/批量操作要么全部生效要么状态不变。
- 输出样本：固定 16kHz，总时长 T 显式给定，输出恰为所有满足
  `0 ≤ n/16000 < T` 的样本（数量 `ceil(16000T)`），区间一律左闭右开，
  不因浮点四舍五入多一个样本。
- 源映射：实数样本位置 `p = a + (t-start)×源采样率×速度`；相邻样本线性插值；
  `p` 到达最后一个样本之后（仍在 clip 时长内）保持末样本值，绝不读区间外。
- 淡化：增益乘 0→1 线性淡入、1→0 线性淡出；淡化时长为 0 时因子恒 1；
  边界用严格小于判定（`d < fade_in`、`remain < fade_out`）。
- **舍入只做一次**：所有有效 clip 的浮点贡献全部相加后，才做“恰半取偶”
  （banker's rounding）一次，再饱和到 `[-32768, 32767]`。单片段先裁剪/先舍入
  再相加是不同语义，本工程不采用。
- 块：每块 1600 样本（0.1s）。块存档记录峰值、实际削顶样本数、所用 clip/asset。

## 复用依据（为什么更正后不会误用旧块）

块键 `block_key = SHA-256(规范JSON{输出采样率, 块序号, 该块重叠到的全部 clip
完整身份与全部转换参数, 这些 clip 引用素材的摘要})`。

- 刻意**不含 product_id、不含 T**：时间轴更正产生新产品时，依赖逐项相同的
  未受影响块仍可复用；块内容与总时长无关。
- 任何重叠 clip 的参数变化或素材正文（摘要）变化都会令键改变 → 新产品不可能
  命中旧块。规模案例中移动一个 clip，1200 块里仅 2 块重渲染、1198 块复用。

## 命令速览（有理数接受 `1/3`、`0.25`、`7` 等写法）

```bash
# 素材登记（序列/斜坡/正弦/静音，自动生成 PCM WAV）
python3 -m audiomix.cli asset-seq --root DIR/registry --rate 16000 --values 0,100,200,300
python3 -m audiomix.cli asset-list --root DIR/registry

# 编辑
python3 -m audiomix.cli tl-insert --root DIR/registry --clip-id c1 \
    --asset-id AID --a 0 --b 4 --start 1/4000 --speed 2 --gain 3/2 \
    --fade-in 1/16000 --fade-out 1/8000
python3 -m audiomix.cli tl-move   --root DIR/registry --clip-id c1 --new-start 1/8000
python3 -m audiomix.cli tl-modify --root DIR/registry --clip-id c1 --set gain=1 --set speed=1
python3 -m audiomix.cli tl-delete --root DIR/registry --clip-id c1
python3 -m audiomix.cli tl-query  --root DIR/registry --lo 0 --hi 1/8000
python3 -m audiomix.cli tl-at     --root DIR/registry --time 1/16000   # 不渲染点查询
python3 -m audiomix.cli tl-publish --root DIR/registry --out product.json

# 校验/渲染/读取/追溯
python3 -m audiomix.cli verify-product --product product.json --registry-root DIR/registry
python3 -m audiomix.cli render-compare --product product.json \
    --registry-root DIR/registry --T 120          # 分块渲染 vs 独立逐样本解释器
python3 -m audiomix.cli render --archive-root DIR/archive --product product.json \
    --registry-root DIR/registry --T 120 --render-id r1
python3 -m audiomix.cli read  --archive-root DIR/archive --render-id r1 --start 0 --end 1600
python3 -m audiomix.cli trace --archive-root DIR/archive --render-id r1 --start 0 --end 3200
python3 -m audiomix.cli export-wav --archive-root DIR/archive --render-id r1 --out out.wav
python3 -m audiomix.cli verify-integrity --archive-root DIR/archive
```

完整端到端示例：`bash demo/cli_walkthrough.sh`；全量验证：`bash run_all_checks.sh`。

## 双盲听审：匿名实包与两序排期

### 刺激制作规则（全部精确整数运算，无浮点）

- 逐样本取旧混音的有符号 16 位整数 PCM，乘各自**批准的正整数增益档**；
- 任一放大样本超出 `[-32768, 32767]`，该 `(增益A, 增益B)` **整个组合退回**，
  不做逐样本削顶；
- 两份放大后序列的**整数平方和必须相等**（音量配平），否则不接受；
- **至少一个对应采样不同**：片段不同但经增益后字节完全相同的两份不得印成问卷；
- 多个合规组合时任选一组（实现取排序后的第一组，确定性可复现）。

### 匿名与封存

- 每份合规组合产出两条只带随机匿名号（无 0/O/1/I 易混字符）的实包，公开目录的
  文件名与 JSON 正文均不含 A/B 或源 render 身份；
- 身份、增益、平方和、样本 SHA-256 摘要只写入 `sealed/trace_table.json` 封存表；
- 持表人可凭摘要验证实包未被替换，并把播放单上的匿名号映射回源 render/片段。

### 排期硬约束（`verify_schedule` 独立校验，与求解器代码分开）

1. 每项完成恰为两场：一场 A 后 B、一场 B 后 A，两序严格相反；
2. 每场占一听者、一台耳机、一个完整时段；同一（听者,时段）与同一（时段,耳机）
   均不得被抢占；
3. 同一人听同项两场，时段号之差 ≥ 2，且**中间整段不听任何节目**（洗耳空档在
   搜索中显式预留，其他项无法插入）；分两人听则记一次“跨人”；
4. 已知道本项任一匿名片真实身份的听者进入该项回避名单，不得安排；
5. 听者只在空闲表时段可用；两场均须不晚于该项最晚结束段；
6. 无法制作刺激或排不齐两序的比较不得计为完成（得 0）。

### 优化目标（字典序、可复现）

- 默认：最大完成赞助权重和 → 跨人项最少 → 完场时段最早；
- 额度模式（给定 `cross_person_quota`）：在额度内取相同权重下**最早完场**一案，
  跨人数作为该模式下的末位比较；
- 规模上限 8 项 / 6 人 / 8 段，回溯搜索穷举，带权重上界剪枝；另用独立暴力枚举
  在 141 个随机小案例上核对最优性，零偏差。

### 命令速览

```bash
# 案例 JSON：n_slots/headphone_capacity/listeners/availability/items(含 PCM 与增益档)
python3 -m audiomix.blindcli solve --case demo/blind_case.json --seed 20261122 \
    --out-dir DIR/trial
# 产物：play_sheets.json（逐人逐时段匿名播放单）、sealed_trace_report.json（封存追溯）、
#       schedule_summary.json（完成项/权重/跨人/完场段/得0项与原因）、
#       stimuli/<项>/{public,sealed}/（匿名实包与封存表）
python3 -m audiomix.blindcli verify --bundle report.json
bash demo/blind_walkthrough.sh     # 含“配不平得0 / 增益后全同拒绝 / 额度模式”的走查
python3 -m tests.test_blind        # 15 个手算案例（纯标准库）
```

## 验证策略（不使用任何外部混音结果）

- `tests/test_hand_calc.py`：短整数序列素材，10 个案例人工推导每个输出样本
  （跨采样率插值、末样本保持、2 倍/半倍速、淡化边界、重叠相消、削顶、
  恰半取偶、跨块边界、整笔拒绝、48k 抽点），并要求**手算值 = 逐样本参考
  解释器 = 分块向量化渲染 = 逐块拼接**四者一致。
- `tests/test_archive.py`：内容寻址复用、移动 clip 的增量渲染、素材摘要变化
  零误用、外部产品篡改拒绝、编辑器日志移走后旧 render 仍可读。
- `tests/test_edge.py`：非整数 T、左闭右开边界、非法修改回滚、持久化重开、
  “先求和后单次舍入”与“逐片段先舍入”的语义区分。
- `tests/test_blind.py`：题面全部情形的 15 个手算案例——配平零跨人换序、跨人早收场、
  配不平得 0、无洗耳空档得 0、增益后字节全同拒绝、两项抢洗耳空段、越界整组退回、
  多增益档确定性、匿名封包零身份信息、知情回避、耳机并行容量、独立校验器拦截伪造、
  权重/跨人/完场三级目标、额度模式、洗耳空档不被插入。
- `demo/scale_demo.py`：30 素材（每采样率 10 个）、100 clip、120 秒/192 万样本，
  含全部要求情形；窗口化逐样本交叉核对，输出 WAV、块峰值、削顶数与来源索引。

参考解释器（`engine.render_reference`）是独立的纯 Python 逐样本 Fraction 实现，
与向量化渲染路径代码完全分开，仅共享产品装载/校验，作为语义基准。
