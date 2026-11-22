# SOLO 流式字符编码服务与原件保真修订库

本题面定义的 SOLO 编码（仅实现题面格式，不接入任何外部编码标准）的 Python 3.11
实现，以及在其之上构建的、按字符区间打补丁但保持未修改原始字节的修订库。

## 交付物

| 路径 | 内容 |
| --- | --- |
| `solo/codec.py` | 流式解码器 `Decoder`、检查点 `Checkpoint`、整件解码 `decode_document` / `decode_all` / `iter_decode`、独立编码入口 `encode_text` / `encode_document`，错误类型与路径记录 |
| `solo/revision.py` | 修订库 `RevisionLibrary`、`replace` / `preserve` 补丁、版本与批次记录、边界模式调整、字节保真校验 |
| `solo/sample.py` | 确定性合成原件生成器（ESC/PAIR 故意跨 128 KiB 块，含模式切换、空替换、连续补丁与非法尾部所需素材） |
| `solo/demo.py` | 演示：真实原件、两批修订、未修改字节核对、错误报告、较大语料，写出 `artifacts/` |
| `solo/board.py` | 填字展板：板稿网格、按原版字节区间定位的覆字与隔线、跨修订格身份、逐项拒收报告、横竖词槽（长度一也算），制板不读词库 |
| `solo/tags.py` | 实体签/谜社/借盒、馆藏新签、限量替签、单场回溯求解与两场连开规划（整盒续借、留柜上限、必挂委托、跨场挂次优化） |
| `solo/exhibit.py` | 用真实 SOLO 原件重现格身份区别（同文重写白格、落进 PAIR 字内部的坏注记、冲突覆字）与馆方两场巡展四个案例，写 `artifacts/exhibit_report.json` |
| `solo/__main__.py` | CLI：`decode/check/encode/revise/demo/exhibit` |
| `tests/test_solo.py` | 独立参考状态机、差分测试、跨块、检查点、修订、规模测试 |
| `tests/test_board.py` | 格身份、半 PAIR/冲突/非白格拒收、隔线相邻性、词槽几何、签号表限额、单场与两场规划（含题面全部案例） |
| `artifacts/` | 运行 `python3 -m solo.demo` 与 `python3 -m solo.exhibit` 生成的真实原件、各版本字节、`manifest.json` 与 `exhibit_report.json` |

## SOLO 编码（题面定义）

- 正文从 `SINGLE` 模式开始。
- `SINGLE`：`00..7F` → 同码点 ASCII；`FF` 为 ESC；`80..FE` 非法。
- ESC 后单字节指令：`01` → 切到 `PAIR`；`00` → 切回 `SINGLE`；`FF` → 字面
  `U+00FF` 且不改模式；其它 → 错误。模式切换本身不产字符。
- `PAIR`：除 `FF`（仍是 ESC）外，两个字节组成无符号大端整数 `n`，映射
  `U+4E00 + n`，限定 `0 ≤ n ≤ 0x0FFF`。第一字节正常时，第二字节即使是 `FF`
  也属于该数值（`00 FF` = n=0x00FF），不会重新开转义。
- 正文结束遇到半个 PAIR 或半条 ESC 均为错误。
- 错误报告原字节偏移与到达该状态的路径（`DecodeError.offset` / `.path`），绝不
  用 `?` 替换后继续。解码失败后解码器进入失败态，不会假成功。

## 流式语义

`Decoder` 按任意字节块增量运行：`feed()` / `feed_into()`，偏移始终是原件正文
偏移。跨块半条 ESC、半个 PAIR 会被保存为待续状态，下一块首字节续接，因此不会
像旧的按块重新初始化解码器那样丢掉块首字符。`finish()` 校验正文末尾并返回结束
模式。

### 检查点

`Decoder.checkpoint()` 返回 `Checkpoint`，包含位置、模式、待续状态与**前缀
SHA-256**；`Checkpoint.resume(body)` / `Decoder.from_checkpoint()` 只在给定正文
前缀摘要一致时才从检查点继续（`verify=False` 可跳过，供已自行校验前缀的调用方
使用）。`Version.checkpoint_at(byte_offset)` 可直接从某个版本继续解码。

## 修订库

- 版本是不可变的真实原件字节 + 有序补丁批次；一个库先经 `add_original()` 载入
  原件（会完整解码校验，失败即拒绝）。
- `replace(start_char, end_char, text)`：按某固定原件版本的**字符**半开区间替换
  为 Unicode 文本。`preserve(start_byte, end_byte)`：要求该**字节**区间原样保留
  （可以落在字符内部；包含它的字符因此不可被替换；若边界落在纯切换字节上则拒绝）。
- 一批补丁先校验：同一基准版本、替换区间两两不相交（相邻允许）、preserve 区间
  互不重叠且不被替换触碰、替换文本可编码；任一失败则整批不发布。
- 组装：未替换字符按原字节复制（`kept_runs` 记录旧字符区间与新字节区间的对应），
  只对替换文本编码；当前输出模式与下一段原始字节解码模式不一致时，才在拼接边界
  插入合法切换（`BoundaryAdjustment`，`splice` / `boundary` / `final`）。整文件
  从不重新编码，因此不会掩盖原件差异。
- 新版本结束模式强制为 `SINGLE`，不会给后续保留字节留下悬空模式。
- 发布前独立校验：重新流式解码新正文，逐段核对未替换区间字节一致、preserve
  区间逐字节一致，并核对解码文本等于“旧 Unicode 做同批替换”的结果。全部通过
  才登记新版本；否则抛出 `RevisionError`，库不变化。

### 版本记录

`Version.records` / `BatchRecord` 记录每个替换的旧字符区间、旧字节区间、新字符
区间、新字节区间、新增字节与边界切换；`boundary_adjustments` 记录新字节偏移、
前后模式和切换字节；`kept_runs` 与 `dropped_switch_spans` 记录原样复制和未保留的
纯切换跨度。

## 填字展板（`board.py` / `tags.py` / `exhibit.py`）

### 板稿与格身份

- 板稿是某修订版本解码出的矩形文本（≤4 行 4 列），行以 `\n` 分隔：`#` 黑格、
  `.` 白格、其余为印字格；每个格身份带原版正文中的完整字符字节区间。
- 跨修订只经补丁记录继承身份：整段复制保留的字符身份延续；凡被 `replace`
  触碰的格身份即失效——**即使替换的是同一个字、新老字节完全相同**（决策依据是
  补丁记录而非字节比对）。
- 覆字按所指原版字节区间定位，且原版那格必须是白格；逐项拒收并报告：
  `bad_range`（对不上完整字符/落在纯切换字节）、`half_pair`（落进双字节 PAIR
  字内部的半个格）、`partial_character`、`not_white`、`identity_rewritten`
  （同文重写也失效）、`conflict`（同格异值，全部不装板）。
- 隔线钉在同一行相邻两格之间；两侧身份都存活、且在新板中仍按原顺序相邻才装板，
  否则报 `side_rewritten` / `not_adjacent`。隔线只阻断左右连词，不影响竖槽。
- `make_board()` 按实际印字与有效覆字推出横竖词槽（黑格与隔线切分，长度一也
  成槽，≤10 槽），全程不读任何谜社词库。

### 签、盒、替签与两场规划

- 实体 `Tag` 带解词、谜社与旧签标记；旧签必须属于某个 `Box`，按整盒交接。
  每槽可挂签 ≤4，一场内同一解词最多一次、每签最多占一槽、每槽实挂一张，
  交叉格必须同字，印字/覆字固定字母必须遵守。
- `ReplacementTag` 另列签号、可替槽与解词，领用即从全单共享库存扣一，不计谜社
  旧签；替签种类 ≤3、借盒 ≤4。
- `feasible_assignments()` 用回溯枚举单场可行挂法；`plan_two_showings()` 处理
  连开两场：首场前各盒到馆，闭馆只续借实际挂出过签的盒（也可退），退回盒次场
  不可借，续借盒内先前没挂的签与已挂过的实体签均可在次场使用（同一旧签两场
  各算一次）。优化次序：最大化目标谜社旧签挂次 → 再保下限谜社 → 少领替签 →
  少占留柜；无解返回 `InfeasibleReport` 指出不能同时兑现的限制。

### 重现案例（`python3 -m solo.exhibit`）

真实 SOLO 原件 `ff01 0000 ff00 2e 0a 2e 44`（板稿“一. / .D”）上：

1. 把白格 `.` 同文重写为 `.`：两版该格字节相同，但旧格身份经补丁记录判定失效，
   钉旧身份的 B 覆字拒收，钉新版本身份的 B 覆字正常装板；
2. `[2,3)` 只取 PAIR 字“一”两个字节中的第一个 → `half_pair` 拒收；
3. 印字 D 格上的覆字 → `not_white`；同格 B/C 两件 → `conflict`；
4. 两场巡展四案例与题面一致：无 R 时首挂 Q1、续乙盒、次挂 Q2（甲 0 乙 2）；
   首场也装 B 覆字则 Q1 不能挂、次场无签；发 R 且必挂 P1 时退乙盒、次场领 R
   （甲 1 乙 0）；无委托时两种借挂方式都成展，由优化目标选择。

## 运行

```bash
python3 -m unittest discover -s tests -v      # 全部测试
python3 -m solo.demo                          # 演示并写 artifacts/
python3 -m solo.exhibit                       # 展板格身份与两场巡展重现
python3 -m solo check artifacts/original.bin
python3 -m solo decode --checkpoint 10 artifacts/version_1.bin
python3 -m solo revise artifacts/original.bin patches.json > new.bin
```

`patches.json` 格式：

```json
{"patches": [
  {"replace": [0, 3, "新文本"]},
  {"preserve": [100, 200]}
]}
```

## 测试覆盖

- 1/2/7/4096 字节分块对独立朴素状态机逐字符、逐字节区间核对（含非法输入与
  错误偏移）。
- 生成器把 ESC/PAIR 精确放在 128 KiB 边界上；跨块、模式切换无字符、替换为空、
  连续/相邻补丁、非法尾部均有专项测试。
- 修订：未修改区间字节等同、每版本可重开解码、失败批次不发布、preserve 逐字节
  保留、新文本等于同批替换、无悬空模式。
- 规模：10 MiB 原件、一批 100 个补丁（`ScaleTests`），未修改运行按原字节复制。
