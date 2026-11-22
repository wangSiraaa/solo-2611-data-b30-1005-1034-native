# 多采样率剪辑时间轴与真实混音档案（audiomix）

纯 Python 3.11 实现（NumPy/SciPy/标准 `wave`），无视频、无音频设备、无网页。
两个独立服务：

1. **可独立编辑的音频时间轴服务**（`audiomix/timeline.py`）：插入/移动/修改/删除/
   区间查询/发布，clip 身份独立于素材，有理数时间，不渲染即可点查询。
2. **实际混音数据仓**（`audiomix/archive.py`）：只消费不可变发布产品与实际素材字节，
   固定 16kHz 输出，按固定样本块存档，内容寻址复用，支持范围读取与逐块追溯；
   不读取编辑器私有操作日志，也接受外部合法时间轴产品。

在此之上还有 **声控接点检测与展台灯箱排班**（`audiomix/lightshow.py`）：
只消费数据仓的真实混音 PCM，逐样本按阈值状态机产生脉冲，再在“必亮拍表 +
自动门 + 值班员手按”约束下求最少手按的走台单。不操作任何外部设备，也不把
目标亮点照抄成结果。

## 目录

```
audiomix/
  rationals.py   精确有理数（Fraction）表示与边界整数运算
  assets.py      PCM WAV 生成/读取 + 素材登记仓（asset_id=正文 SHA-256，绑定摘要/样本数）
  timeline.py    时间轴编辑器（整笔校验、点查询、不可变发布产品）
  engine.py      混音引擎：逐样本参考解释器 + 等价的 NumPy 分块向量化渲染
  archive.py     混音数据仓：内容寻址块存储 + SQLite 依赖索引 + 增量渲染 + 读取/追溯
  lightshow.py   声控接点检测（跨块不重置）+ 自动门/手按最少手按派工
  cli.py         命令行入口
tests/           手算案例、数据仓案例、边界语义、声控走台测试
demo/            30 素材/100 clip/120s 规模案例、声控走台样例、CLI 端到端走查
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

# 声控走台（接点检测 -> 走台单；仅消费真实渲染 PCM）
python3 -m audiomix.cli lights-pulses --archive-root DIR/archive \
    --contacts "pickup:r1:2:0:1:1"            # id:render:H:L:n_on:n_off
python3 -m audiomix.cli lights-plan --archive-root DIR/archive \
    --contacts "pickup:r1:2:0:1:1" \
    --required "L1@1,L1@5" --roster "1=alice;5=alice" --gates 1 --beats-total 7
python3 -m audiomix.cli lights-lock-query --archive-root DIR/archive \
    --render-id r1 --beat 1850 --H 50 --L 10 --n-on 1800 --n-off 1
```

## 声控接点与展台排班语义（`lightshow.py`）

- **接点状态机**：整数启动阈 H、复位阈 L（`0 <= L < H`）、连续启动样本数 `n_on`、
  连续复位样本数 `n_off`。初态可启动；未锁时连续 `|x| >= H` 达 `n_on` 即在最后一拍
  发一个脉冲并上锁；上锁后连续 `|x| <= L` 达 `n_off` 才复位。中间值 `L < |x| < H`
  既不计启动也不计复位，并打断未完成的连续段。逐块 `feed` 不重置任何状态。
- **脉冲来源与锁定原因**：每个 `Pulse` 记录 `contact_id / render_id / beat /
  run_start / run_end / value`；每个 `LockEvent` 记录触发原因与 `unbeat`（复位拍）。
  `lock_explanation(n)` 可查“第 n 拍为何仍被锁住”。
- **自动门**：把一只接点接到一盏灯，在连续闭区间内照传该接点全部脉冲。一条接线
  合法的充要条件是：**窗口内每个脉冲都恰是该灯当拍的必亮点**，否则会在禁亮拍误亮。
  同灯同拍只许真正点亮一次（同灯两门不得在重叠拍上重复点灯）。
- **排班目标**：给定自动门数量，求 (未覆盖数, 手按次数, 用门数) 字典序最小的走台单；
  同分少用门。自动门整场最多各用一次。余下必亮点由当拍有空的值班员手按，一人一拍
  至多一盏。无人能补的必亮点逐条列入 `uncovered`，绝不漏报。规模上限：4 灯、3 接点、
  12 必亮点。

完整端到端示例：`bash demo/cli_walkthrough.sh`，声控样例：`bash demo/light_demo.sh`；
全量验证：`bash run_all_checks.sh`。

## 验证策略（不使用任何外部混音结果）

- `tests/test_hand_calc.py`：短整数序列素材，10 个案例人工推导每个输出样本
  （跨采样率插值、末样本保持、2 倍/半倍速、淡化边界、重叠相消、削顶、
  恰半取偶、跨块边界、整笔拒绝、48k 抽点），并要求**手算值 = 逐样本参考
  解释器 = 分块向量化渲染 = 逐块拼接**四者一致。
- `tests/test_archive.py`：内容寻址复用、移动 clip 的增量渲染、素材摘要变化
  零误用、外部产品篡改拒绝、编辑器日志移走后旧 render 仍可读。
- `tests/test_edge.py`：非整数 T、左闭右开边界、非法修改回滚、持久化重开、
  “先求和后单次舍入”与“逐片段先舍入”的语义区分。
- `tests/test_lightshow.py`：连续触发/复位计数、中间值打断、绝对值阈值、锁定原因
  查询、跨块不重置；自动门合法窗、禁亮拍绝不误亮、同灯同拍只点一次、两灯同拍争
  一人、无门无人时显式列未覆盖。
- `demo/scale_demo.py`：30 素材（每采样率 10 个）、100 clip、120 秒/192 万样本，
  含全部要求情形；窗口化逐样本交叉核对，输出 WAV、块峰值、削顶数与来源索引。
- `demo/light_demo.py`：短 WAV 真实渲染 -> 数据仓实际 PCM -> 逐样本检测 -> 走台单。
  覆盖题面五例（基础一门一手按、加 -3 相消后长门零手按、固定原档两门零手按、
  两灯同拍争一人、持续高音跨块边界）。

参考解释器（`engine.render_reference`）是独立的纯 Python 逐样本 Fraction 实现，
与向量化渲染路径代码完全分开，仅共享产品装载/校验，作为语义基准。
