"""保存部 + 装订间：残本整配。

「版心」问题与目录别名是两件事
--------------------------------
目录别名（catalog）只决定两部书目是否是同一**规范作品**。印次
（impression）与版心（page_block）是实物版本身份：即使 WA、WB 在某个
修订里被合并为同一作品，只要印次没有声明为「相容」，两个不同版心也绝不
能当作同一版互换书帖。因此供本判定同时要求：

1. 供本与主本的原书目在**当前修订**下解析到同一规范作品；
2. 供本的印次与主本印次**相同或显式相容**（impression_compat）。

题记与不可动帖
--------------
`gatherings.immovable=1` 表示带读者题记或其他必须原位保留的实帖：
它只能留在本主本的原有位置，既不能挪去别的位置，也不能从供本拆走。

书帖不能拆页两用
----------------
每个候选帖按页段整体匹配；匹配到某段就整帖装上，同一实物帖在整批方案中
至多出现一次（全局唯一）。不允许把一帖拆成两半分别装到两册。

边码连续
--------
印次登记各位置的页段次序与起讫边码；相邻两段共享一个边界码，即

* 页段相接：prev.page_end + 1 == next.page_start
* 边码相接：prev.edge_end == next.edge_start（共享边界，如 a→x 后接 x→z）

首帖的首码须等于印次声明的首码，末帖的尾码须等于声明的尾码（如「以 z
收尾」）。单个缺段即使各自都有帖可补，若拼起来共享边界对不上，整册仍判
退件（`no_continuous_assembly`）。

真实资料不足
------------
印次未登记 `page_block`（或主本实物无印次）时，无法判定版心，方案标
`needs_verification`，且不得确认整配单。
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterator

from . import db
from .catalog import CatalogService
from .errors import Rejection

BLOCK_UNVERIFIED = "needs_verification"

# 一次联合搜索的节点上限，防止极端数据把试排拖死（题目规模远小于此）
_MAX_SEARCH_NODES = 200_000


# ---------------------------------------------------------------------------
# 值域与书帖匹配
# ---------------------------------------------------------------------------

_ALPHA = re.compile(r"^[a-z]+$")
_SIGNED = re.compile(r"^([+-]?)(\d+)([+-]?)$")


def _alpha_value(text: str) -> int | None:
    """a=1 … z=26，aa=27 …；仅用于给边码一个可比较的序号。"""
    text = text.strip().lower()
    if not _ALPHA.match(text):
        return None
    value = 0
    for ch in text:
        value = value * 26 + (ord(ch) - ord("a") + 1)
    return value


def _signed_value(text: str) -> int | None:
    """接受 '+12'、'12+'、'-3' 这类带符号数值边码。"""
    m = _SIGNED.match(text.strip())
    if not m:
        return None
    sign = -1 if (m.group(1) == "-" or m.group(3) == "-") else 1
    return sign * int(m.group(2))


def _edge_value(raw: str | None, cues: dict[str, int] | None = None) -> int | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if cues and text in cues:
        return cues[text]
    return _alpha_value(text) if _ALPHA.match(text.lower()) else _signed_value(text)


@dataclass(frozen=True)
class Segment:
    """一个位置要求的连续页段与首尾边码。"""

    seq: int
    page_start: int
    page_end: int
    edge_start: str
    edge_end: str


@dataclass(frozen=True)
class Gathering:
    id: str
    copy_id: str
    seq: int
    page_start: int
    page_end: int
    edge_start: str
    edge_end: str
    immovable: bool
    fee: int


@dataclass(frozen=True)
class Facsimile:
    id: str
    impression_id: str
    seq: int
    page_start: int
    page_end: int
    edge_start: str
    edge_end: str
    cost: int
    qty: int


def _norm(raw: str, cues: dict[str, int] | None = None) -> Any:
    """规范化边码：能解析出数值就用数值，否则退化为原字符串比较。"""
    value = _edge_value(raw, cues)
    return value if value is not None else str(raw).strip().lower()


def _covers(seg: Segment, page_start: int, page_end: int) -> bool:
    """整帖必须恰好覆盖该位置的页段（实帖不能拆页两用）。"""
    return page_start == seg.page_start and page_end == seg.page_end


def _joins(prev_edges: tuple[str, str], next_edges: tuple[str, str],
           cues: dict[str, int] | None = None) -> bool:
    """相邻两帖是否首尾对上：后一帖的首码等于前一帖的尾码（共享边界）。"""
    return _norm(prev_edges[1], cues) == _norm(next_edges[0], cues)


def _opens_at(seg: Segment, edges: tuple[str, str],
              cues: dict[str, int] | None = None) -> bool:
    """全册开头必须从印次声明的首码起。"""
    return _norm(edges[0], cues) == _norm(seg.edge_start, cues)


def _closes_at(seg: Segment, edges: tuple[str, str],
               cues: dict[str, int] | None = None) -> bool:
    """全册结尾必须收在印次声明的尾码（如「以 z 收尾」）。"""
    return _norm(edges[1], cues) == _norm(seg.edge_end, cues)


# ---------------------------------------------------------------------------
# 候选
# ---------------------------------------------------------------------------

@dataclass
class Candidate:
    uid: str               # 全局唯一资源 ID
    kind: str              # 'stock' | 'supply' | 'fac'
    seg: Segment
    cost: int
    gathering: Gathering | None = None
    facsimile: Facsimile | None = None
    unit: int = 0          # 仿印份号（同一 facsimile 的多份各自唯一）

    @property
    def edges(self) -> tuple[str, str]:
        if self.gathering is not None:
            return (self.gathering.edge_start, self.gathering.edge_end)
        assert self.facsimile is not None
        return (self.facsimile.edge_start, self.facsimile.edge_end)

    def describe(self) -> dict[str, Any]:
        base: dict[str, Any] = {"kind": self.kind, "cost": self.cost}
        if self.kind == "fac":
            assert self.facsimile is not None
            base["facsimile"] = self.facsimile.id
            base["facsimile_unit"] = self.unit
        else:
            assert self.gathering is not None
            base["gathering"] = self.gathering.id
            base["donor"] = self.gathering.copy_id
        return base


@dataclass
class MainRequest:
    copy_id: str
    impression_id: str | None
    min_originals: int = 0
    require_facsimile: bool = False


@dataclass
class _MainContext:
    main: MainRequest
    impression_id: str | None
    segments: list[Segment]
    cues: dict[str, int] | None
    candidates: list[list[Candidate]]        # 每个位置可用的候选
    preference: dict[int, str]               # 位置 -> 指定的 resource uid
    needs_verification: bool
    occupied_hold: dict[str, Any] | None
    impressions_ok: bool


# ---------------------------------------------------------------------------
# 服务
# ---------------------------------------------------------------------------

class BinderyService:
    def __init__(self, conn: sqlite3.Connection, catalog: CatalogService) -> None:
        self.conn = conn
        self.catalog = catalog

    # ================================================================ 登记

    def register_copy(self, copy_id: str, book: str, branch: str,
                      impression_id: str | None = None) -> dict[str, Any]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            if self.conn.execute(
                "SELECT 1 FROM bindery_copies WHERE copy_id=?", (copy_id,)
            ).fetchone():
                raise Rejection("bindery_copy_exists", copy=copy_id)
            if impression_id is not None and not self.conn.execute(
                "SELECT 1 FROM impressions WHERE impression_id=?", (impression_id,)
            ).fetchone():
                raise Rejection("unknown_impression", impression=impression_id)
            self.conn.execute(
                "INSERT INTO bindery_copies(copy_id, book, impression_id, branch)"
                " VALUES(?, ?, ?, ?)",
                (copy_id, book, impression_id, branch),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return {"status": "ok", "copy": copy_id}

    def register_impression(
        self,
        impression_id: str,
        page_block: str | None,
        segments: list[dict[str, Any]],
        compatible: list[str] | None = None,
    ) -> dict[str, Any]:
        """登记一个印次：页段次序、起讫边码、获认可的相容印次。

        `page_block=None` 表示实物资料不足以判版心，查询时会标待核。
        """
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            if self.conn.execute(
                "SELECT 1 FROM impressions WHERE impression_id=?", (impression_id,)
            ).fetchone():
                raise Rejection("impression_exists", impression=impression_id)
            ordered = sorted(segments, key=lambda s: s["seq"])
            if not ordered:
                raise Rejection("impression_without_segments",
                                impression=impression_id)
            for s in ordered:
                if s["page_end"] < s["page_start"]:
                    raise Rejection(
                        "bad_page_range", impression=impression_id, at=s["seq"]
                    )
            # 页段与边码必须前后相接，否则这个印次本身不自洽：
            # 页段连续；相邻两段共享边界码（前段尾码 == 后段首码）
            for prev, nxt in zip(ordered, ordered[1:]):
                if prev["page_end"] + 1 != nxt["page_start"]:
                    raise Rejection(
                        "impression_page_gap", impression=impression_id,
                        at=nxt["seq"],
                    )
                if _norm(prev["edge_end"]) != _norm(nxt["edge_start"]):
                    raise Rejection(
                        "impression_edge_gap", impression=impression_id,
                        at=nxt["seq"],
                    )
            self.conn.execute(
                "INSERT INTO impressions(impression_id, page_block) VALUES(?, ?)",
                (impression_id, page_block),
            )
            self.conn.executemany(
                "INSERT INTO impression_segments(impression_id, seq, page_start, "
                "page_end, edge_start, edge_end) VALUES(?, ?, ?, ?, ?, ?)",
                [
                    (impression_id, s["seq"], s["page_start"], s["page_end"],
                     s["edge_start"], s["edge_end"])
                    for s in ordered
                ],
            )
            for other in compatible or []:
                if other == impression_id:
                    raise Rejection("self_compatibility", impression=impression_id)
                self.conn.execute(
                    "INSERT OR IGNORE INTO impression_compat"
                    "(impression_id, compatible_id) VALUES(?, ?)",
                    (impression_id, other),
                )
                self.conn.execute(
                    "INSERT OR IGNORE INTO impression_compat"
                    "(impression_id, compatible_id) VALUES(?, ?)",
                    (other, impression_id),
                )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return {"status": "ok", "impression": impression_id}

    def add_gathering(
        self,
        gathering_id: str,
        copy_id: str,
        seq: int,
        page_start: int,
        page_end: int,
        edge_start: str,
        edge_end: str,
        immovable: bool = False,
        fee: int = 0,
    ) -> dict[str, Any]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            if self.conn.execute(
                "SELECT 1 FROM bindery_gatherings WHERE gathering_id=?",
                (gathering_id,),
            ).fetchone():
                raise Rejection("gathering_exists", gathering=gathering_id)
            if not self.conn.execute(
                "SELECT 1 FROM bindery_copies WHERE copy_id=?", (copy_id,)
            ).fetchone():
                raise Rejection("unknown_bindery_copy", copy=copy_id)
            if fee < 0:
                raise Rejection("negative_fee", gathering=gathering_id)
            if page_end < page_start:
                raise Rejection("bad_page_range", gathering=gathering_id)
            self.conn.execute(
                "INSERT INTO bindery_gatherings(gathering_id, copy_id, seq, "
                "page_start, page_end, edge_start, edge_end, immovable, fee)"
                " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (gathering_id, copy_id, seq, page_start, page_end,
                 edge_start, edge_end, 1 if immovable else 0, fee),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return {"status": "ok", "gathering": gathering_id}

    def add_facsimile(
        self,
        facsimile_id: str,
        impression_id: str,
        seq: int,
        page_start: int,
        page_end: int,
        edge_start: str,
        edge_end: str,
        cost: int,
        qty: int = 1,
    ) -> dict[str, Any]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            if qty < 0:
                raise Rejection("negative_qty", facsimile=facsimile_id)
            if cost < 0:
                raise Rejection("negative_cost", facsimile=facsimile_id)
            if page_end < page_start:
                raise Rejection("bad_page_range", facsimile=facsimile_id)
            if self.conn.execute(
                "SELECT 1 FROM facsimiles WHERE facsimile_id=?", (facsimile_id,)
            ).fetchone():
                raise Rejection("facsimile_exists", facsimile=facsimile_id)
            if not self.conn.execute(
                "SELECT 1 FROM impressions WHERE impression_id=?", (impression_id,)
            ).fetchone():
                raise Rejection("unknown_impression", impression=impression_id)
            self.conn.execute(
                "INSERT INTO facsimiles(facsimile_id, impression_id, seq, "
                "page_start, page_end, edge_start, edge_end, cost, qty)"
                " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (facsimile_id, impression_id, seq, page_start, page_end,
                 edge_start, edge_end, cost, qty),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return {"status": "ok", "facsimile": facsimile_id}

    # ================================================================ 试排 / 整配

    def plan_repair(self, copy_id: str, reminder_bindings: dict[int, str] | None = None
                    ) -> dict[str, Any]:
        """对单个主本试排：不拆实物，不占用供本，只给方案与退件原因。"""
        ctx = self._build_context(
            MainRequest(copy_id=copy_id, impression_id=None),
            extra_gatherings=None,
            preference=reminder_bindings or {},
        )
        if ctx is None:
            return self._rejection("unknown_bindery_copy", copy=copy_id)
        if self.conn.execute(
            "SELECT 1 FROM copy_repair_state WHERE copy_id=?", (copy_id,)
        ).fetchone():
            return self._rejection("copy_in_repair", copy=copy_id)
        plans = self._solve([ctx], force_facsimile=False)
        if ctx.needs_verification:
            # 版心无法判定：标待核，不给出可确认的方案
            out: dict[str, Any] = {
                "status": BLOCK_UNVERIFIED,
                "copy": copy_id,
                "reason": "版心资料不足，无法判定所需页段次序与边码",
                "tentative": None,
            }
            if plans:
                best, _ = min(
                    plans, key=lambda p: (p[0]["cost"], p[0]["originals"])
                )
                out["tentative"] = self._finish_single(ctx, best)
            return out
        if not plans:
            return self._explain_failure(ctx)
        best, _ = min(plans, key=lambda p: (p[0]["cost"], p[0]["originals"]))
        result = self._finish_single(ctx, best)
        result["claims"] = self._claims(ctx, best)
        return result

    def plan_order(self, order_id: str, mains: list[dict[str, Any]],
                   force_facsimile: bool = False,
                   confirm: bool = False) -> dict[str, Any]:
        """对一批主本联合试排（默认不拆实物、不占用），求最低费用方案。

        同一实物帖 / 同一仿印单元在整批中全局唯一，绝不被两册同时安装。
        `force_facsimile=True` 用于展示“用仿印保住供本”的替代方案。
        """
        if len(mains) > 4:
            return self._rejection("too_many_mains", count=len(mains))
        reqs = [
            MainRequest(
                copy_id=m["copy"],
                impression_id=m.get("impression"),
                min_originals=int(m.get("min_originals", 0)),
                require_facsimile=bool(m.get("require_facsimile", False)),
            )
            for m in mains
        ]
        ctxs: list[_MainContext] = []
        for req, raw in zip(reqs, mains):
            ctx = self._build_context(
                req,
                extra_gatherings=raw.get("gatherings"),
                preference={
                    int(k): v for k, v in (raw.get("bindings") or {}).items()
                },
            )
            if ctx is None:
                return self._rejection("unknown_bindery_copy", copy=req.copy_id)
            if len(ctx.segments) > 6:
                return self._rejection("too_many_positions", copy=req.copy_id)
            total_cands = sum(len(c) for c in ctx.candidates)
            if total_cands > 20:
                return self._rejection(
                    "too_many_candidates", copy=req.copy_id, count=total_cands
                )
            ctxs.append(ctx)

        # 仿印已在 _facsimiles_for 中按 qty 展开为独立单元，全局唯一
        plans = self._solve(ctxs, force_facsimile=force_facsimile)
        if not plans:
            # 先看每册独立是否可行：有独立就不可行的，报最具体原因；
            # 各册独立都可行、合起来却无解，说明是跨册资源争用。
            independent_ok = all(bool(self._solve([c], force_facsimile)) for c in ctxs)
            if independent_ok:
                return self._rejection(
                    "no_joint_assembly", order=order_id,
                    detail="各册单独可补，但同一实物帖/仿印不能同时装两册",
                )
            for c in ctxs:
                r = self._explain_failure(c)
                if r.get("status") == "rejected":
                    r["order"] = order_id
                    return r
            return self._rejection("no_feasible_assembly", order=order_id)

        plan, assignments = min(
            plans, key=lambda p: (p[0]["cost"], p[0]["originals"])
        )
        detail = self._describe_plan(ctxs, plan, assignments)
        detail["order"] = order_id

        needs_verification = any(c.needs_verification for c in ctxs)
        if needs_verification:
            detail["status"] = BLOCK_UNVERIFIED
        if confirm:
            if needs_verification:
                return self._rejection(
                    "needs_verification", order=order_id
                )
            self._store_order(order_id, detail, needs_verification)
            return self.confirm_order(order_id)

        self._store_order(order_id, detail, needs_verification)
        return detail

    # ---------------------------------------------------------------- 上下文

    def _build_context(
        self,
        req: MainRequest,
        extra_gatherings: list[str] | None,
        preference: dict[int, str],
    ) -> _MainContext | None:
        copy = self.conn.execute(
            "SELECT * FROM bindery_copies WHERE copy_id=?", (req.copy_id,)
        ).fetchone()
        if copy is None:
            return None
        impression_id = req.impression_id or copy["impression_id"]
        impression = None
        if impression_id is not None:
            impression = self.conn.execute(
                "SELECT * FROM impressions WHERE impression_id=?", (impression_id,)
            ).fetchone()
        impressions_ok = impression is not None
        if impression is None:
            # 无印次 = 实物资料不足，无法判版心：位置由已登记书帖的足迹推断
            segments = self._infer_segments(req.copy_id, extra_gatherings or [])
            needs_verification = True
        else:
            segments = [
                Segment(
                    seq=r["seq"], page_start=r["page_start"],
                    page_end=r["page_end"], edge_start=r["edge_start"],
                    edge_end=r["edge_end"],
                )
                for r in self.conn.execute(
                    "SELECT * FROM impression_segments WHERE impression_id=? "
                    "ORDER BY seq", (impression_id,),
                )
            ]
            needs_verification = impression["page_block"] is None

        candidates: list[list[Candidate]] = [[] for _ in segments]

        # 候选：主本自身的现存帖（stock，原位安装，不付费）
        own_rows = [
            self._row_to_gathering(r)
            for r in self.conn.execute(
                "SELECT * FROM bindery_gatherings WHERE copy_id=? ORDER BY seq",
                (req.copy_id,),
            )
        ]
        for gid in extra_gatherings or []:
            row = self.conn.execute(
                "SELECT * FROM bindery_gatherings WHERE gathering_id=?", (gid,)
            ).fetchone()
            if row is not None:
                own_rows.append(self._row_to_gathering(row))
        for seg_idx, seg in enumerate(segments):
            for g in own_rows:
                if not _covers(seg, g.page_start, g.page_end):
                    continue
                if g.immovable and g.seq != seg.seq:
                    continue  # 题记帖不得移位
                candidates[seg_idx].append(Candidate(
                    uid=f"g:{g.id}", kind="stock", seg=seg, cost=0,
                    gathering=g,
                ))

        # 候选：相容印次、同一规范作品的供本（supply，可从供本拆下）
        for g in self._eligible_donor_gatherings(
            req.copy_id, impression_id, impression
        ):
            for seg_idx, seg in enumerate(segments):
                if _covers(seg, g.page_start, g.page_end):
                    candidates[seg_idx].append(Candidate(
                        uid=f"g:{g.id}", kind="supply", seg=seg,
                        cost=g.fee, gathering=g,
                    ))

        # 候选：可购买的仿印帖（fac，有实际边码）；每份各自唯一
        for f, unit in self._facsimiles_for(impression_id):
            for seg_idx, seg in enumerate(segments):
                if _covers(seg, f.page_start, f.page_end):
                    candidates[seg_idx].append(Candidate(
                        uid=f"f:{f.id}#{unit}", kind="fac", seg=seg,
                        cost=f.cost, facsimile=f, unit=unit,
                    ))

        occupied = self._hold_on_copy(req.copy_id)
        return _MainContext(
            main=req, impression_id=impression_id, segments=segments, cues=None,
            candidates=candidates, preference=preference,
            needs_verification=needs_verification,
            occupied_hold=occupied, impressions_ok=impressions_ok,
        )

    def _infer_segments(self, copy_id: str,
                        extra: list[str]) -> list[Segment]:
        """资料不足时用现有帖的页段拼出位置（仅用于展示待核方案）。"""
        rows = self.conn.execute(
            "SELECT * FROM bindery_gatherings WHERE copy_id=? ORDER BY seq",
            (copy_id,),
        ).fetchall()
        segments = [
            Segment(r["seq"], r["page_start"], r["page_end"],
                    r["edge_start"], r["edge_end"])
            for r in rows
        ]
        return sorted(segments, key=lambda s: s.seq)

    def _row_to_gathering(self, r: sqlite3.Row) -> Gathering:
        return Gathering(
            id=r["gathering_id"], copy_id=r["copy_id"], seq=r["seq"],
            page_start=r["page_start"], page_end=r["page_end"],
            edge_start=r["edge_start"], edge_end=r["edge_end"],
            immovable=bool(r["immovable"]), fee=r["fee"],
        )

    def _impression_compatible(self, a: str | None, b: str | None) -> bool:
        if a is None or b is None:
            return False
        if a == b:
            return True
        return self.conn.execute(
            "SELECT 1 FROM impression_compat WHERE impression_id=? "
            "AND compatible_id=?", (a, b),
        ).fetchone() is not None

    def _same_work(self, book_a: str, book_b: str) -> bool:
        rev = self.catalog.head_revision
        if not rev:
            return book_a == book_b
        try:
            canon = self.catalog.canonical_map(rev)
        except Rejection:
            return book_a == book_b
        return canon.get(book_a, book_a) == canon.get(book_b, book_b)

    def _eligible_donor_gatherings(
        self, main_copy: str, impression_id: str | None,
        impression: sqlite3.Row | None,
    ) -> Iterator[Gathering]:
        """同一规范作品 + 相容印次 + 供本未被保留/借出（且不在拆装）。"""
        main = self.conn.execute(
            "SELECT * FROM bindery_copies WHERE copy_id=?", (main_copy,)
        ).fetchone()
        if main is None:
            return
        for row in self.conn.execute(
            "SELECT * FROM bindery_copies WHERE copy_id<>?", (main_copy,)
        ):
            donor = row
            if not self._same_work(main["book"], donor["book"]):
                continue
            donor_imp = donor["impression_id"]
            if not self._impression_compatible(impression_id, donor_imp):
                continue
            # 供本不能已被保留/借出，也不能正在拆装
            if self._occupancy(donor["copy_id"]) is not None:
                continue
            if self.conn.execute(
                "SELECT 1 FROM copy_repair_state WHERE copy_id=?",
                (donor["copy_id"],),
            ).fetchone():
                continue
            for g_row in self.conn.execute(
                "SELECT * FROM bindery_gatherings WHERE copy_id=? ORDER BY seq",
                (donor["copy_id"],),
            ):
                g = self._row_to_gathering(g_row)
                if g.immovable:
                    continue  # 题记帖不可从供本移走
                yield g

    def _facsimiles_for(
        self, impression_id: str | None
    ) -> Iterator[tuple[Facsimile, int]]:
        """可购仿印；每册（按 qty）产生多个独立单元保证跨册不共用。"""
        rows = self.conn.execute(
            "SELECT * FROM facsimiles WHERE qty > 0 ORDER BY facsimile_id"
        ).fetchall()
        for r in rows:
            if not self._impression_compatible(impression_id, r["impression_id"]):
                continue
            f = Facsimile(
                id=r["facsimile_id"], impression_id=r["impression_id"],
                seq=r["seq"], page_start=r["page_start"],
                page_end=r["page_end"], edge_start=r["edge_start"],
                edge_end=r["edge_end"], cost=r["cost"], qty=r["qty"],
            )
            for unit in range(f.qty):
                yield f, unit

    def _hold_on_copy(self, copy_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT hold_id, ticket_id, reader_id, branch, status "
            "FROM reservations WHERE copy_id=? AND status IN ('held','fulfilled') "
            "ORDER BY rowid LIMIT 1", (copy_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def _occupancy(self, copy_id: str) -> dict[str, Any] | None:
        return self._hold_on_copy(copy_id)

    # ---------------------------------------------------------------- 求解

    def _solve(self, ctxs: list[_MainContext],
               force_facsimile: bool) -> list[tuple[dict[str, Any], dict]]:
        """联合深度优先：逐册逐位置安装，资源全局唯一。返回全部可行方案。"""
        # 每册候选排序：指定绑定优先，然后原件（stock 免费、supply 付费）、仿印最后
        for ctx in ctxs:
            for i, cands in enumerate(ctx.candidates):
                pref = ctx.preference.get(i)
                cands.sort(key=lambda c: (
                    0 if (pref is not None and c.uid == pref) else 1,
                    0 if c.kind == "stock" else 1,
                    0 if c.kind == "supply" else 1,
                    c.cost,
                    c.uid,
                ))

        plans: list[tuple[dict[str, Any], dict]] = []
        nodes = [0]

        def walk(mi: int, pi: int, used: set[str],
                 chosen: list[list[Candidate]], running_cost: int) -> None:
            nodes[0] += 1
            if nodes[0] > _MAX_SEARCH_NODES:
                return
            # 只保留当前最优；费用非负，已超最优费用即可剪枝
            if plans:
                best_cost = plans[0][0]["cost"]
                if running_cost > best_cost:
                    return
                # 费用已达理论最小值 0：不可能更省，停止搜索
                if best_cost == 0:
                    return
            if mi == len(ctxs):
                self._maybe_record(ctxs, chosen, force_facsimile, plans)
                return
            ctx = ctxs[mi]
            if pi == len(ctx.segments):
                walk(mi + 1, 0, used, chosen, running_cost)
                return
            seg = ctx.segments[pi]
            pref = ctx.preference.get(pi)
            for c in ctx.candidates[pi]:
                if c.uid in used:
                    continue
                if pref is not None and c.uid != pref:
                    continue
                if pi > 0:
                    prev = chosen[mi][pi - 1]
                    if prev is None:
                        continue
                    if not _joins(prev.edges, c.edges):
                        continue
                else:
                    # 全册起点必须从印次声明的首码起（若有）
                    if not _opens_at(seg, c.edges):
                        continue
                if pi == len(ctx.segments) - 1:
                    # 全册终点必须落在声明的尾码（若有）
                    if not _closes_at(seg, c.edges):
                        continue
                chosen[mi][pi] = c
                used.add(c.uid)
                walk(mi, pi + 1, used, chosen, running_cost + c.cost)
                used.remove(c.uid)
                chosen[mi][pi] = None

        walk(0, 0, set(), [[None] * len(c.segments) for c in ctxs], 0)

        plans.sort(key=lambda p: (p[0]["cost"], p[0]["originals"]))

        plans.sort(key=lambda p: (p[0]["cost"], p[0]["originals"]))
        return plans

    def _maybe_record(self, ctxs: list[_MainContext],
                      chosen: list[list[Candidate]], force_facsimile: bool,
                      out: list[tuple[dict[str, Any], dict]]) -> None:
        total_cost = 0
        total_originals = 0
        per: dict[str, dict[str, Any]] = {}
        for ctx, picks in zip(ctxs, chosen):
            originals = sum(1 for c in picks if c.kind in ("stock", "supply"))
            facs = sum(1 for c in picks if c.kind == "fac")
            if originals < ctx.main.min_originals:
                return
            if ctx.main.require_facsimile and facs == 0:
                return
            if force_facsimile and facs == 0:
                return
            cost = sum(c.cost for c in picks)
            total_cost += cost
            total_originals += originals
            per[ctx.main.copy_id] = {
                "picks": list(picks), "cost": cost,
                "originals": originals, "facsimiles": facs,
            }
        candidate = (
            {"cost": total_cost, "originals": total_originals, "per": per},
            per,
        )
        # 所有调用方都只要最低费用方案（同分再比原件数），
        # 因此内存里只保留当前最优，避免大量可行解撑爆内存。
        if out:
            best = out[0]
            if (candidate[0]["cost"], candidate[0]["originals"]) >= (
                best[0]["cost"], best[0]["originals"]
            ):
                return
            out.clear()
        out.append(candidate)

    # ---------------------------------------------------------------- 结果

    def _describe_plan(self, ctxs: list[_MainContext],
                       plan: dict[str, Any], assignments: dict) -> dict[str, Any]:
        mains_out = []
        for ctx in ctxs:
            info = assignments[ctx.main.copy_id]
            picks: list[Candidate] = info["picks"]
            positions = []
            stock, supply, fac = [], [], []
            donors: dict[str, list[str]] = {}
            for seg, c in zip(ctx.segments, picks):
                entry = {
                    "seq": seg.seq,
                    "pages": f"{seg.page_start}-{seg.page_end}",
                    "edges": f"{c.edges[0]}->{c.edges[1]}",
                    **c.describe(),
                }
                positions.append(entry)
                if c.kind == "stock":
                    stock.append({"gathering": c.gathering.id, "seq": c.gathering.seq})
                elif c.kind == "supply":
                    assert c.gathering is not None
                    from_copy = c.gathering.copy_id
                    supply.append({
                        "gathering": c.gathering.id,
                        "from_copy": from_copy,
                        "donor_seq": c.gathering.seq,
                    })
                    donors.setdefault(from_copy, []).append(c.gathering.id)
                else:
                    fac.append({
                        "facsimile": c.facsimile.id,
                        "cost": c.facsimile.cost,
                    })
            mains_out.append({
                "copy": ctx.main.copy_id,
                "impression": ctx.impression_id,
                "status": BLOCK_UNVERIFIED if ctx.needs_verification else "ok",
                "positions": positions,
                "stock": stock,
                "supply": supply,
                "facsimiles_used": fac,
                "originals": info["originals"],
                "min_originals": ctx.main.min_originals,
                "cost": info["cost"],
                "holds_preserved": ctx.occupied_hold,
                "donors_lose": [
                    {"donor": d, "lost_gatherings": gs}
                    for d, gs in donors.items()
                ],
            })
        return {
            "status": "ok",
            "cost": plan["cost"],
            "originals": plan["originals"],
            "mains": mains_out,
        }

    def _finish_single(self, ctx: _MainContext,
                       plan: dict[str, Any]) -> dict[str, Any]:
        detail = self._describe_plan([ctx], plan, plan["per"])
        detail["cost"] = plan["cost"]
        if ctx.needs_verification:
            detail["status"] = BLOCK_UNVERIFIED
        return detail

    def _claims(self, ctx: _MainContext, plan: dict[str, Any]) -> dict[str, Any]:
        """保存员视图：逐位置列出可用的完整实帖，并标明原位保留与相邻接续。"""
        picks = plan["per"][ctx.main.copy_id]["picks"]
        by_seg: dict[int, dict[str, Any]] = {}
        for i, seg in enumerate(ctx.segments):
            options = []
            for c in ctx.candidates[i]:
                if c.kind == "fac":
                    options.append({
                        "facsimile": c.facsimile.id,
                        "kind": "fac",
                        "fee": c.cost,
                    })
                    continue
                g = c.gathering
                assert g is not None
                options.append({
                    "gathering": g.id,
                    "from_copy": g.copy_id,
                    "kind": c.kind,
                    "fee": c.cost,
                    "must_stay": bool(g.immovable),
                })
            joins_prev = None
            joins_next = None
            if i > 0:
                joins_prev = sorted({
                    c.gathering.id for c in ctx.candidates[i]
                    if any(_joins(p.edges, c.edges) for p in ctx.candidates[i - 1])
                    and c.gathering is not None
                })
            if i < len(ctx.segments) - 1:
                joins_next = sorted({
                    c.gathering.id for c in ctx.candidates[i]
                    if any(_joins(c.edges, n.edges) for n in ctx.candidates[i + 1])
                    and c.gathering is not None
                })
            chosen = picks[i]
            by_seg[seg.seq] = {
                "pages": f"{seg.page_start}-{seg.page_end}",
                "edges": f"{seg.edge_start}->{seg.edge_end}",
                "can_fill": options,
                "chosen": chosen.describe(),
                "joins_previous": joins_prev,
                "joins_next": joins_next,
            }
        return by_seg

    def _explain_failure(self, ctx: _MainContext) -> dict[str, Any]:
        """给出最具体的退件原因：缺帖 / 开头尾部边码 / 相邻边码不连续。"""
        segs = ctx.segments
        for i, seg in enumerate(segs):
            if not ctx.candidates[i]:
                return self._rejection(
                    "uncovered_segment", copy=ctx.main.copy_id, seq=seg.seq,
                    pages=f"{seg.page_start}-{seg.page_end}",
                    edges=f"{seg.edge_start}->{seg.edge_end}",
                )
        if not segs:
            return self._rejection("no_positions", copy=ctx.main.copy_id)
        # 逐段做可达性检查（只看本册、忽略跨册唯一性），定位断点
        reach = [c for c in ctx.candidates[0] if _opens_at(segs[0], c.edges)]
        if not reach:
            return self._rejection(
                "no_continuous_assembly", copy=ctx.main.copy_id,
                at_seq=segs[0].seq,
                detail="开头书帖的首码与印次声明的首码对不上",
            )
        for i in range(1, len(segs)):
            nxt = [
                c for c in ctx.candidates[i]
                if any(_joins(p.edges, c.edges) for p in reach)
            ]
            if not nxt:
                prev_seg, next_seg = segs[i - 1], segs[i]
                return self._rejection(
                    "no_continuous_assembly",
                    copy=ctx.main.copy_id, at_seq=next_seg.seq,
                    detail=(
                        "每个缺段单独都有帖可补，但拼起来相邻两段边码不连续"
                        if prev_seg.page_end + 1 == next_seg.page_start
                        else "相邻两段页段不相接"
                    ),
                )
            reach = nxt
        if not any(_closes_at(segs[-1], c.edges) for c in reach):
            return self._rejection(
                "no_continuous_assembly", copy=ctx.main.copy_id,
                at_seq=segs[-1].seq,
                detail="末帖的尾码与印次声明的尾码对不上",
            )
        # 边码层面有可行链，但原件数等要求不满足
        return self._rejection(
            "original_requirement_unmet", copy=ctx.main.copy_id,
            min_originals=ctx.main.min_originals,
        )

    def _rejection(self, reason: str, **detail: object) -> dict[str, Any]:
        return {"status": "rejected", "error": {"reason": reason, **detail}}

    # ---------------------------------------------------------------- 落库

    def _store_order(self, order_id: str, detail: dict[str, Any],
                     needs_verification: bool) -> None:
        """记录试排单；不拆实物、不占用供本。重开后可查。"""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            prior = self.conn.execute(
                "SELECT status FROM repair_orders WHERE order_id=?", (order_id,)
            ).fetchone()
            if prior is not None and prior["status"] in ("confirmed", "completed"):
                raise Rejection("order_locked", order=order_id)
            self.conn.execute(
                "INSERT INTO repair_orders(order_id, revision, status, plan, "
                "created_at) VALUES(?, ?, ?, ?, ?) "
                "ON CONFLICT(order_id) DO UPDATE SET revision=excluded.revision, "
                "status=excluded.status, plan=excluded.plan",
                (order_id, self.catalog.head_revision,
                 BLOCK_UNVERIFIED if needs_verification else "planned",
                 json.dumps(detail, ensure_ascii=False), db.now_iso()),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def confirm_order(self, order_id: str) -> dict[str, Any]:
        """确认整配单：置主本为“正在拆装”，并从供本实际移走书帖。

        此后借还入口拒绝正在拆装的实物；旧 hold 的人和副本不变。
        """
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute(
                "SELECT * FROM repair_orders WHERE order_id=?", (order_id,)
            ).fetchone()
            if row is None:
                raise Rejection("unknown_order", order=order_id)
            if row["status"] == "completed":
                raise Rejection("order_completed", order=order_id)
            if row["status"] == BLOCK_UNVERIFIED:
                raise Rejection("needs_verification", order=order_id)
            plan = json.loads(row["plan"])
            # 重新核对资源仍可用（供本未被动过、仿印数量足够）
            self._revalidate(plan)
            for m in plan["mains"]:
                cid = m["copy"]
                if self.conn.execute(
                    "SELECT 1 FROM copy_repair_state WHERE copy_id=?", (cid,)
                ).fetchone():
                    raise Rejection("copy_already_in_repair", copy=cid)
            for m in plan["mains"]:
                for sup in m["supply"]:
                    # 实物帖从供本移入主本：供本实际少了这一帖
                    self.conn.execute(
                        "UPDATE bindery_gatherings SET copy_id=? WHERE gathering_id=?",
                        (m["copy"], sup["gathering"]),
                    )
                # 记录最终实帖次序：装上的实帖按方案位置落位
                for pos in m["positions"]:
                    gid = pos.get("gathering")
                    if gid is None:
                        continue  # 仿印不落 gatherings 表
                    self.conn.execute(
                        "UPDATE bindery_gatherings SET seq=? WHERE gathering_id=?",
                        (pos["seq"], gid),
                    )
                for fac in m["facsimiles_used"]:
                    self.conn.execute(
                        "UPDATE facsimiles SET qty=qty-1 WHERE facsimile_id=?",
                        (fac["facsimile"],),
                    )
                self.conn.execute(
                    "INSERT OR REPLACE INTO copy_repair_state(copy_id, state, "
                    "order_id) VALUES(?, 'in_repair', ?)",
                    (m["copy"], order_id),
                )
            self.conn.execute(
                "UPDATE repair_orders SET status='confirmed', confirmed_at=? "
                "WHERE order_id=?", (db.now_iso(), order_id),
            )
            self.conn.commit()
        except Rejection as rej:
            self.conn.rollback()
            return {"status": "rejected", "error": rej.as_dict()}
        except Exception:
            self.conn.rollback()
            raise
        return {"status": "ok", "order": order_id, "state": "confirmed",
                "plan": plan}

    def complete_order(self, order_id: str) -> dict[str, Any]:
        """完工：写永久完工清单，主本交回原馆、解除拆装；旧 hold 仍可兑现。"""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute(
                "SELECT * FROM repair_orders WHERE order_id=?", (order_id,)
            ).fetchone()
            if row is None:
                raise Rejection("unknown_order", order=order_id)
            if row["status"] != "confirmed":
                raise Rejection("order_not_confirmed", order=order_id,
                                state=row["status"])
            plan = json.loads(row["plan"])
            detail = {"plan": plan, "completed_at": db.now_iso()}
            for m in plan["mains"]:
                extra = self.conn.execute(
                    "SELECT branch FROM bindery_copies WHERE copy_id=?",
                    (m["copy"],),
                ).fetchone()
                m["returned_to_branch"] = extra["branch"] if extra else None
                self.conn.execute(
                    "DELETE FROM copy_repair_state WHERE copy_id=?", (m["copy"],)
                )
            self.conn.execute(
                "INSERT OR REPLACE INTO repair_completions(order_id, detail, "
                "completed_at) VALUES(?, ?, ?)",
                (order_id, json.dumps(detail, ensure_ascii=False), db.now_iso()),
            )
            self.conn.execute(
                "UPDATE repair_orders SET status='completed', completed_at=? "
                "WHERE order_id=?", (db.now_iso(), order_id),
            )
            self.conn.commit()
        except Rejection as rej:
            self.conn.rollback()
            return {"status": "rejected", "error": rej.as_dict()}
        except Exception:
            self.conn.rollback()
            raise
        return {"status": "ok", "order": order_id, "state": "completed",
                "completion": detail}

    def _revalidate(self, plan: dict[str, Any]) -> None:
        fac_need_all: dict[str, int] = {}
        for m in plan["mains"]:
            for sup in m["supply"]:
                g = self.conn.execute(
                    "SELECT copy_id FROM bindery_gatherings WHERE gathering_id=?",
                    (sup["gathering"],),
                ).fetchone()
                if g is None or g["copy_id"] != sup.get("from_copy"):
                    raise Rejection(
                        "stale_plan", copy=m["copy"],
                        gathering=sup["gathering"], reason="gathering_moved",
                    )
            for fac in m["facsimiles_used"]:
                fid = fac["facsimile"]
                fac_need_all[fid] = fac_need_all.get(fid, 0) + 1
        for fid, need in fac_need_all.items():
            f = self.conn.execute(
                "SELECT qty FROM facsimiles WHERE facsimile_id=?", (fid,)
            ).fetchone()
            if f is None or f["qty"] < need:
                raise Rejection("stale_plan", facsimile=fid,
                                reason="insufficient_qty")
        # 同一实物帖不得被两册同时安装（跨册全局唯一）
        seen: set[str] = set()
        for m in plan["mains"]:
            for sup in m["supply"]:
                gid = sup["gathering"]
                if gid in seen:
                    raise Rejection("stale_plan", gathering=gid,
                                    reason="double_use")
                seen.add(gid)

    # ---------------------------------------------------------------- 查询

    def get_order(self, order_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM repair_orders WHERE order_id=?", (order_id,)
        ).fetchone()
        if row is None:
            raise Rejection("unknown_order", order=order_id)
        out = dict(row)
        out["plan"] = json.loads(out["plan"]) if out["plan"] else None
        return out

    def get_completion(self, order_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM repair_completions WHERE order_id=?", (order_id,)
        ).fetchone()
        return json.loads(row["detail"]) if row is not None else None

    def get_copy_state(self, copy_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM copy_repair_state WHERE copy_id=?", (copy_id,)
        ).fetchone()
        return {"copy": copy_id, "in_repair": row is not None,
                "order": row["order_id"] if row is not None else None}

    def get_gatherings(self, copy_id: str) -> list[dict[str, Any]]:
        return [
            self._row_to_gathering(r).__dict__
            for r in self.conn.execute(
                "SELECT * FROM bindery_gatherings WHERE copy_id=? ORDER BY seq",
                (copy_id,),
            )
        ]
