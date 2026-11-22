# 多采样率剪辑时间轴与真实混音档案（audiomix）

纯 Python 3.11 实现（NumPy/SciPy/标准 `wave`），无视频、无音频设备、无网页。
两个独立服务与一个校准求解器：

1. **可独立编辑的音频时间轴服务**（`audiomix/timeline.py`）：插入/移动/修改/删除/
   区间查询/发布，clip 身份独立于素材，有理数时间，不渲染即可点查询。
2. **实际混音数据仓**（`audiomix/archive.py`）：只消费不可变发布产品与实际素材字节，
   固定 16kHz 输出，按固定样本块存档，内容寻址复用，支持范围读取与逐块追溯；
   不读取编辑器私有操作日志，也接受外部合法时间轴产品。
3. **试听厅校准与最低安装费求解**（`audiomix/hall.py`）：整数卷积精确反卷积

   + 板况分柜的校验录音仓 + 以拍为单位的全厅离散模型，求让全部座位听到且无重影
   的最低安装费，输出交接线、屏板、各座实响序列与真实 WAV 试听。

## 目录

```
audiomix/
  rationals.py   精确有理数（Fraction）表示与边界整数运算
  assets.py      PCM WAV 生成/读取 + 素材登记仓（asset_id=正文 SHA-256，绑定摘要/样本数）
  timeline.py    时间轴编辑器（整笔校验、点查询、不可变发布产品）
  engine.py      混音引擎：逐样本参考解释器 + 等价的 NumPy 分块向量化渲染
  archive.py     混音数据仓：内容寻址块存储 + SQLite 依赖索引 + 增量渲染 + 读取/追溯
  hall.py        试听厅：整数反卷积 + 板况分柜录音仓 + 最低安装费求解 + 试听 WAV
  cli.py         命令行入口
tests/           手算案例、数据仓案例、边界语义、试听厅校准测试
demo/            30 素材/100 clip/120s 规模案例、试听厅两排试听席案例与 CLI 走查
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

# 试听厅校准（整数反卷积 / 交叉验证 / 最低安装费）
python3 -m audiomix.cli hall-deconv --registry-root DIR/registry \
    --excitation XID --observation YID --max-len 3       # 单路还原，失败给首处不符
python3 -m audiomix.cli hall-validate --registry-root DIR/registry \
    --speaker R --seat X --state bare \
    --record R,X,bare,XID1,YID1,3 --record R,X,bare,XID2,YID2,3  # 另一真实试音片交叉验证
python3 -m audiomix.cli hall-solve --registry-root DIR/registry \
    --scenario scenario.json --out result.json --wav-dir out --summary
```

完整端到端示例：`bash demo/cli_walkthrough.sh`、`python3 -m demo.hall_demo`；
全量验证：`bash run_all_checks.sh`。

## 试听厅校准语义（`hall.py`）

- **激励/观测**：激励是从具名原剪辑发布并已渲染的短 PCM，`x[0] != 0`；观测是
  另一份整段实录。模型为本地离散整数卷积 `y = x * h`（完整线性卷积，含末尾零），
  响应长度上限 `H` 已知。
- **精确整数反卷积**：逐拍长除还原有符号整数 `h[0..H-1]`。绝不把截断、舍入、
  饱和或超长尾巴解释成“另一次反射”；若不能逐样本用长度 `H` 的整数响应还原，
  返回首个不符的**观测样本下标**与类别（`truncated`/`non_integer`/`tail_mismatch`/
  `extra_tail`/`bad_excitation`）。
- **板况分柜**：每份校验录音绑定（喇叭, 座位, 板况）；`recover` 只从同板况取记录，
  开板/闭板两套记录**不可互借**（缺记录直接报错）。可用另一真实试音片交叉验证同一路。
- **全厅离散模型**：各喇叭可任选其明列的一种工作状态（裸机/装自己的板）与非负整数
  电延迟，各带安装费。延迟把该喇叭到所有座位的整条响应整体右移；装板换用该机对
  各座位已核出的相应响应。某座同拍全部有符号贡献**先求和**，`|和| >= 门限` 才算响；
  同拍正负响应相消为 0，不算两声。每座第一声不得晚于本座 `latest_onset`；第一声后
  拍差超过 `allowed_gap` 仍有达门限声响即重影。遍历全部 (状态, 延迟) 组合求最低总费用。
- **交付物**：屏板（各喇叭状态/延迟/费用）、交接线（每座逐拍先求和明细及达门限标记）、
  各座实响序列、真实 WAV 试听（每座 + 混合；加窗正弦，峰值远离 16 位饱和）。

## 验证策略（不使用任何外部混音结果）

- `tests/test_hand_calc.py`：短整数序列素材，10 个案例人工推导每个输出样本
  （跨采样率插值、末样本保持、2 倍/半倍速、淡化边界、重叠相消、削顶、
  恰半取偶、跨块边界、整笔拒绝、48k 抽点），并要求**手算值 = 逐样本参考
  解释器 = 分块向量化渲染 = 逐块拼接**四者一致。
- `tests/test_archive.py`：内容寻址复用、移动 clip 的增量渲染、素材摘要变化
  零误用、外部产品篡改拒绝、编辑器日志移走后旧 render 仍可读。
- `tests/test_edge.py`：非整数 T、左闭右开边界、非法修改回滚、持久化重开、
  “先求和后单次舍入”与“逐片段先舍入”的语义区分。
- `tests/test_hall.py`：真实例 `[1,1]*[2,1]=[2,3,1]`；含末尾零与超长尾；截断/非整数/
  尾部不符各给首处不符；同拍正负相消不算两声；板况记录不可互借；另一真实试音片交叉
  验证；缺记录/反卷积失败/不可行三类异常；主场景最低费 2、换实测 0、装板 3。
- `demo/hall_demo.py`：激励经时间轴发布并渲染（首样本非零，故也能用精确反卷积还原）；
  两排试听席端到端求最低费、打印屏板/交接线/各座实响、导出并校验真实 WAV。
- `demo/scale_demo.py`：30 素材（每采样率 10 个）、100 clip、120 秒/192 万样本，
  含全部要求情形；窗口化逐样本交叉核对，输出 WAV、块峰值、削顶数与来源索引。

参考解释器（`engine.render_reference`）是独立的纯 Python 逐样本 Fraction 实现，
与向量化渲染路径代码完全分开，仅共享产品装载/校验，作为语义基准。
