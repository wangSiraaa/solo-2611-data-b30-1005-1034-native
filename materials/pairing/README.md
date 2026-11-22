# 联展配对名册合同与认同单文件

两家分馆各持半张名册，联展方（报名台/发放台）经 extcontract 原接入口汇合，任一分馆都拿不到整份配对名册。

## 报名台合同（认同单）

认同单按 `row_id` 为报名号，同一报名号在两边各有一行：行修订（`revision`）、馆内成员号（`member_no`）、展组号（`group_no`）、轮次（`round`）、签认（`consent`，`同意`/`撤回`）。

- `desk-left-reader.json`（`pairdesk-left-r1`）/ `desk-right-reader.json`（`pairdesk-right-r1`）：报名台两个方向的固定读者合同，即冻结版本；`consent` 列带读者默认 `同意`。
- `left-form-writer.json`（`left-form-w1`）：左馆写者，`member_no` 为 TEXT；展示名（会员号/展组号/轮次/签认）与读者不同，配对只认稳定 `field_id`。
- `right-form-writer.json`（`right-form-w1`）：右馆写者，`member_no` 为 INT8，读者域 INT16（成员号按真实读者域解释）。
- `right-form-writer-legacy.json`（`right-form-w0`）：右馆旧写者，**缺 consent 列**；读者默认补出的 `同意` 不是本人签认，该报名待签。

规则：同一报名号须两边当前单各自独立成立（真实列报出 `同意`、非默认补出、非 `撤回`），且同轮次同展组才汇合；不得拿两笔同组报名的左右端拼一项。一对可受多笔报名支持，撤到最后一份支持才撤出。

## 认同单文件（示例情形）

- `forms-left-1.json` / `forms-right-1.json`：reg-a1(a,1)、reg-a2(a,2)、reg-b2(b,2) 双方真实签认。
- `forms-right-legacy.json`：reg-b1 右方旧合同登记，consent 为缺列补的默认 → (b,1) 待签。
- `forms-right-2.json`：reg-b1 右方 rev2 用真实 consent 列签认 → 四对全有。
- `forms-left-x1/x2/x3.json`、`forms-right-x1.json`：reg-x1、reg-x2 同支持 (c,7)；先撤 reg-x1 仍保留，再撤 reg-x2 才撤出。

## 发放台合同（两份名单）

发放台只分送两个文件，除此之外没有补发的对照表：

- `issue-left-writer.json` → `issue-left-reader.json`（`left-branch-r1`）：左馆领（组 `group_no`、左成员 `member_no`、章号 `stamp_no`）。
- `issue-right-writer.json` → `issue-right-reader.json`（`right-branch-r1`）：右馆领（组、右成员、章号），右成员读者域 INT16。

同组同章配成一对，重复命中归一。一枚章可盖多个左端和多个右端，但不许漏一对，也不许让原来不同的人凭同章误配。每人一张纸卡跨组合计章位；全场景另有限定刻章总数；章不跨组，一枚章不得虚印成两个章号。目标：最少刻章数，两份名单确能合回签出的真实二元关系，并出具复联见证。

端到端演示：`PYTHONPATH=. python3.11 examples/run_pairing_demo.py`。
