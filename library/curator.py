"""策展人：轮展班表的可行性搜索与最低费用选择。

输入：一场展出的规范作品、天数、每日可读最低亮度、换本费与遮光片日费。
动作只有三种（每场每日只展一本，其他可用本留在暗处）：
- 首本进场免费；之后换另一册实际进场收一次换本费；
- 正常照度 2；装遮光片降到 1（须仍达当日最低），每个遮光使用日收遮光费；
- 不展的册子当天按暗藏恢复：P 不动，R 减该册暗藏恢复量到最低 0。

硬约束（全展期每一天对每一册都必须成立，不能把几册余度加起来当一册）：
- 展册当日受光后 P<=P_cap 且 R<=R_cap；暗册恢复后 R>=0；
- 有有效保留或实际未还借阅的副本不得签成可展；
- 遮光只能在 1 仍达当日最低时使用。

搜索在题目上限（<=6 册、<=10 日）内穷举“每日展本 × 照度”，用记忆化
按 (日, 各册 P/R) 求最低费用后缀，并保留全部并列最优序列；
正式选定后才登记逐册占期与实际受光，重复签发不会再多耗一次光额度。
"""

from __future__ import annotations

import heapq
import json
from typing import Any

from . import db
from .errors import Rejection


class CuratorService:
    def __init__(self, conn, catalog, conservation) -> None:
        self.conn = conn
        self.catalog = catalog
        self.conservation = conservation
        self.ledger = conservation.ledger

    # ================================================================ 规划

    def plan_exhibition(
        self,
        canonical_bib: str,
        min_levels: list[int],
        swap_fee: int,
        shade_fee: int,
        revision: str | None = None,
        start_day: int | None = None,
    ) -> dict[str, Any]:
        """纯计算：返回全部并列最低费用的可兑现班表（不落库）。

        start_day 是开展的日历日（全局整数日序）。默认接在该作品全部候选册
        已签读数日的次日；每册先从馆藏初始状态重放到 start_day-1（含其间暗藏
        恢复），用真实双账作为班表第 0 日的起点，旧借阅的受光因此如实压顶。
        """
        revision = revision or self.catalog.head_revision
        self._validate_params(min_levels, swap_fee, shade_fee)
        canon = self.catalog.canonical_map(revision)
        if canonical_bib not in set(canon.values()):
            raise Rejection(
                "unknown_canonical_bib", canonical_bib=canonical_bib
            )
        candidates = self._eligible_copies(revision, canon, canonical_bib)
        if not candidates:
            raise Rejection(
                "no_exhibitable_copy", canonical_bib=canonical_bib
            )
        profiles = {
            cid: self.conservation.get_profile(cid) for cid in candidates
        }
        if start_day is None:
            start_day = self._default_start_day(candidates, profiles)
        else:
            earliest = min(p["anchor_day"] for p in profiles.values())
            if start_day < earliest:
                raise Rejection(
                    "start_day_before_anchor",
                    start_day=start_day, anchor=earliest,
                )
        # 开展前一日的真实双账：含全部已签认日光与期间暗藏恢复
        init_state = {
            cid: self._state_before_day(profiles[cid], start_day)
            for cid in candidates
        }
        days = len(min_levels)
        result = self._search(
            candidates, profiles, init_state, min_levels,
            swap_fee, shade_fee, days,
        )
        if result is None:
            raise Rejection(
                "no_feasible_schedule",
                canonical_bib=canonical_bib, days=days,
            )
        min_cost, schedules = result
        return {
            "status": "ok",
            "revision": revision,
            "canonical_bib": canonical_bib,
            "days": days,
            "start_day": start_day,
            "min_levels": list(min_levels),
            "swap_fee": swap_fee,
            "shade_fee": shade_fee,
            "candidate_copies": candidates,
            "initial_state": init_state,
            "minimum_cost": min_cost,
            "schedule_count": len(schedules),
            "schedules": [
                self._render_schedule(s, profiles, min_levels,
                                      swap_fee, shade_fee, init_state,
                                      start_day)
                for s in schedules
            ],
        }

    def _default_start_day(self, candidates, profiles) -> int:
        """开展日默认接在全部候选册已签读数日的次日（无读数则用锚点日）。"""
        latest = min(p["anchor_day"] for p in profiles.values())
        for cid in candidates:
            for reading in self.conservation.signed_readings(cid):
                latest = max(latest, reading["day"] + 1)
        return latest

    def _state_before_day(self, profile: dict, day: int) -> tuple[int, int]:
        """该册在日历日 day 开始前（即 day-1 日终）的真实 P/R。"""
        if day <= profile["anchor_day"]:
            return self.conservation.initial_state(profile)
        return self.conservation._replay_until(profile, day - 1)

    # ================================================================ 选定登记

    def register_exhibition(
        self,
        op_id: str,
        expo_id: str | None,
        schedule: dict[str, Any],
    ) -> dict[str, Any]:
        """正式选定：登记场次、逐日班表、逐册占期与实际受光。

        登记瞬间按当前目录与台账重新验证可展性；同 op_id 同文重放原样返回，
        异文冲突；已被旧入口真正占走（held/fulfilled）的册不得签成可展。
        """
        payload = {"expo_id": expo_id, "schedule": schedule}
        fp = db.fingerprint("register_exhibition", payload)
        conn = self.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            prior = db.check_idempotent(conn, op_id, "register_exhibition", fp)
            if prior is not None:
                conn.commit()
                return prior
            revision = schedule["revision"]
            bib = schedule["canonical_bib"]
            min_levels = schedule["min_levels"]
            swap_fee = schedule["swap_fee"]
            shade_fee = schedule["shade_fee"]
            start_day = schedule["start_day"]
            entries = schedule["entries"]
            self._validate_params(min_levels, swap_fee, shade_fee)
            if self.catalog.head_revision != revision:
                raise Rejection(
                    "not_head_revision",
                    revision=revision, head=self.catalog.head_revision,
                )
            canon = self.catalog.canonical_map(revision)
            eligible = set(
                self._eligible_copies(revision, canon, bib)
            )
            for e in entries:
                if e["copy"] not in eligible:
                    # 已被旧入口真正占走的册不得签成可展
                    raise Rejection(
                        "copy_not_exhibitable", copy=e["copy"], day=e["day"]
                    )
            # 独立重放（同一开展日），确保提交的班表确实满足实物双限且费用自洽
            planned = self.plan_exhibition(
                bib, min_levels, swap_fee, shade_fee, revision, start_day
            )
            if not self._schedule_among(entries, planned["schedules"]):
                raise Rejection("schedule_not_optimal_or_invalid")
            real_id = expo_id or self._next_expo_id(conn)
            if conn.execute(
                "SELECT 1 FROM exhibitions WHERE expo_id=?", (real_id,)
            ).fetchone():
                raise Rejection("expo_exists", expo=real_id)
            ts = db.now_iso()
            conn.execute(
                "INSERT INTO exhibitions(expo_id, revision, canonical_bib, "
                "days, start_day, min_levels, swap_fee, shade_fee, status, "
                "total_cost, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'registered', ?, ?)",
                (real_id, revision, bib, len(min_levels), start_day,
                 json.dumps(min_levels), swap_fee, shade_fee,
                 planned["minimum_cost"], ts),
            )
            rendered = next(
                s for s in planned["schedules"]
                if self._same_entries(s["entries"], entries)
            )
            for e, rd in zip(entries, rendered["entries"]):
                calendar_day = rd["calendar_day"]
                conn.execute(
                    "INSERT INTO exhibition_entries(expo_id, day, calendar_day, "
                    "copy_id, light, shaded, day_cost, end_p, end_r) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (real_id, e["day"], calendar_day, e["copy"], e["light"],
                     1 if e["shaded"] else 0, rd["day_cost"],
                     rd["end_p"], rd["end_r"]),
                )
                conn.execute(
                    "INSERT INTO exhibition_occupancy(expo_id, day, "
                    "calendar_day, copy_id, light) VALUES(?, ?, ?, ?, ?)",
                    (real_id, e["day"], calendar_day, e["copy"], e["light"]),
                )
                # 实际受光进入保存员的逐册履历（来源标注本场展出，落在真实
                # 日历日）；同册同日的唯一索引保证重复签发不多耗一份光额度
                conn.execute(
                    "INSERT INTO light_readings(copy_id, day, exposure, source, "
                    "state, op_id, created_at) VALUES(?, ?, ?, ?, 'signed', ?, ?)",
                    (e["copy"], calendar_day, e["light"], f"expo:{real_id}",
                     op_id, ts),
                )
            result = {
                "status": "ok",
                "expo_id": real_id,
                "total_cost": planned["minimum_cost"],
                "entries": entries,
                "end_state": rendered["end_state"],
            }
            db.record_operation(conn, op_id, "register_exhibition", fp)
            db.add_receipt(conn, "register_exhibition", "ok", result, op_id)
            conn.commit()
            return result
        except Rejection as rej:
            conn.rollback()
            return self._reject(op_id, fp, rej)
        except Exception:
            conn.rollback()
            raise

    def get_exhibition(self, expo_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM exhibitions WHERE expo_id=?", (expo_id,)
        ).fetchone()
        if row is None:
            raise Rejection("unknown_expo", expo=expo_id)
        entries = [
            dict(r) for r in self.conn.execute(
                "SELECT day, calendar_day, copy_id AS copy, light, shaded, "
                "day_cost, end_p, end_r FROM exhibition_entries "
                "WHERE expo_id=? ORDER BY day",
                (expo_id,),
            )
        ]
        out = dict(row)
        out["min_levels"] = json.loads(out["min_levels"])
        out["entries"] = entries
        return out

    def list_exhibitions(self) -> list[dict[str, Any]]:
        return [
            dict(r) for r in self.conn.execute(
                "SELECT expo_id, canonical_bib, days, status, total_cost "
                "FROM exhibitions ORDER BY rowid"
            )
        ]

    def occupancy(self, copy_id: str) -> list[dict[str, Any]]:
        """该册被各场展出占走的日历日与实际受光（旧入口的真实占期）。"""
        return [
            dict(r) for r in self.conn.execute(
                "SELECT expo_id, day, calendar_day, light "
                "FROM exhibition_occupancy WHERE copy_id=? ORDER BY day", (copy_id,)
            )
        ]

    # ================================================================ 搜索

    def _search(
        self, candidates, profiles, init_state, min_levels,
        swap_fee, shade_fee, days,
    ) -> tuple[int, list[list[dict[str, Any]]]] | None:
        """按“日-状态”图上的 Dijkstra 求最低费用（题目上限 <=6 册、<=10 日）。

        节点是 (日序, 各册 (P,R) 快照, 上一展本)，边是某天选某册、某照度的
        动作，边权为该日换本费/遮光费。每个节点只在首次（最低费用）到达时
        定型，并保留全部“以相同最低费用到达它”的父边用于回溯；
        终点的最短距离即最低总费用，父边森林枚举全部并列最优班表。
        余度始终按册独立核算，从不跨册相加。
        """
        cands = list(candidates)

        def freeze(states: dict, previous: str | None, idx: int):
            return (
                idx,
                tuple(sorted(
                    (c, states[c][0], states[c][1]) for c in cands
                )),
                previous,
            )

        def unfreeze(node):
            _idx, state_tuple, previous = node
            return {c: (p, r) for c, p, r in state_tuple}, previous

        def step(idx: int, states: dict, copy: str, light: int):
            """返回新状态；不可行（低于当日最低或超双限）返回 None。"""
            profile = profiles[copy]
            p0, r0 = states[copy]
            if light < min_levels[idx]:
                return None  # 遮光后仍须达当日最低
            if p0 + light > profile["p_cap"]:
                return None
            if r0 + light > profile["r_cap"]:
                return None
            next_states = {}
            for c in cands:
                p, r = states[c]
                if c == copy:
                    next_states[c] = (p + light, r + light)
                else:
                    # 其他可用本全天在暗处
                    next_states[c] = (
                        p, max(0, r - profiles[c]["dark_recovery"])
                    )
            return next_states

        def action_cost(previous, copy, light):
            swapped = previous is not None and previous != copy
            return (swap_fee if swapped else 0) + (
                shade_fee if light == 1 else 0
            )

        start = freeze(dict(init_state), None, 0)
        dist = {start: 0}
        # parents[node] = [(父 node, 动作)]，仅保留落在最短距离上的父边
        parents: dict[tuple, list[tuple]] = {start: []}
        heap = [(0, start)]
        terminal_best: int | None = None

        while heap:
            d, node = heapq.heappop(heap)
            if d != dist[node]:
                continue
            idx = node[0]
            if idx == days:
                terminal_best = d
                break
            states, previous = unfreeze(node)
            for copy in cands:
                for light in (2, 1):
                    nxt_states = step(idx, states, copy, light)
                    if nxt_states is None:
                        continue
                    nxt_node = freeze(nxt_states, copy, idx + 1)
                    edge = action_cost(previous, copy, light)
                    nd = d + edge
                    if nd < dist.get(nxt_node, 10 ** 12):
                        dist[nxt_node] = nd
                        action = {
                            "day": idx, "copy": copy, "light": light,
                            "shaded": light == 1,
                            "swapped": previous is not None and previous != copy,
                            "day_cost": edge,
                        }
                        parents[nxt_node] = [(node, action)]
                        heapq.heappush(heap, (nd, nxt_node))
                    elif nd == dist.get(nxt_node):
                        action = {
                            "day": idx, "copy": copy, "light": light,
                            "shaded": light == 1,
                            "swapped": previous is not None and previous != copy,
                            "day_cost": edge,
                        }
                        parents[nxt_node].append((node, action))

        if terminal_best is None:
            return None
        terminals = [n for n in dist if n[0] == days and dist[n] == terminal_best]

        def backtrack(node):
            if node == start:
                yield []
                return
            for parent, action in parents.get(node, []):
                for rest in backtrack(parent):
                    yield rest + [action]

        paths = []
        for node in terminals:
            paths.extend(backtrack(node))
        paths = sorted(
            paths,
            key=lambda path: tuple((a["copy"], a["light"]) for a in path),
        )
        return terminal_best, paths

    # ================================================================ 渲染/校验

    def _render_schedule(
        self, actions, profiles, min_levels, swap_fee, shade_fee,
        init_state, start_day,
    ) -> dict[str, Any]:
        """把动作序列重放成逐日班表：展本、照度、暗藏恢复、日终双账。

        states 以开展前一日的真实双账为起点；条目同时给展期内序号 day 与
        全局日历日 calendar_day。
        """
        states = dict(init_state)
        entries = []
        for action in actions:
            day = action["day"]
            copy = action["copy"]
            light = action["light"]
            dark_notes = {}
            for c in profiles:
                if c == copy:
                    continue
                p, r = states[c]
                nr = max(0, r - profiles[c]["dark_recovery"])
                dark_notes[c] = {"recovered_drop": r - nr, "end_r": nr}
            p, r = states[copy]
            states[copy] = (p + light, r + light)
            for c in profiles:
                if c != copy:
                    pp, rr = states[c]
                    states[c] = (
                        pp, max(0, rr - profiles[c]["dark_recovery"])
                    )
            entries.append({
                "day": day,
                "calendar_day": start_day + day,
                "copy": copy,
                "light": light,
                "shaded": action["shaded"],
                "swapped": action["swapped"],
                "minimum": min_levels[day],
                "day_cost": action["day_cost"],
                "end_p": states[copy][0],
                "end_r": states[copy][1],
                "dark_recovery_of_others": dark_notes,
            })
        end_state = {
            c: {
                "p": states[c][0], "r": states[c][1],
                "p_cap": profiles[c]["p_cap"], "r_cap": profiles[c]["r_cap"],
                "p_headroom": profiles[c]["p_cap"] - states[c][0],
                "r_headroom": profiles[c]["r_cap"] - states[c][1],
            }
            for c in profiles
        }
        total = sum(e["day_cost"] for e in entries)
        return {
            "entries": entries,
            "total_cost": total,
            "end_state": end_state,
        }

    def _eligible_copies(self, revision, canon, canonical_bib) -> list[str]:
        """规范作品下、有光照账且当前无 held/fulfilled 占用的实物册。"""
        out = []
        for st in self.ledger.get_copy_statuses(revision):
            if st["occupied"]:
                continue  # 有效保留或实际未还：不能擅入展
            if canon.get(st["original_bib"]) != canonical_bib:
                continue
            if self.conn.execute(
                "SELECT 1 FROM light_profiles WHERE copy_id=?",
                (st["copy_id"],),
            ).fetchone() is None:
                continue  # 没有光照账的册无法核双限，不入候选
            out.append(st["copy_id"])
        return sorted(out)

    @staticmethod
    def _validate_params(min_levels, swap_fee, shade_fee) -> None:
        if not min_levels:
            raise Rejection("empty_schedule")
        for i, m in enumerate(min_levels):
            if not isinstance(m, int) or m not in (1, 2):
                raise Rejection("bad_min_level", day=i, level=m)
        for name, val in (("swap_fee", swap_fee), ("shade_fee", shade_fee)):
            if not isinstance(val, int) or val < 0:
                raise Rejection("bad_fee", param=name, value=val)

    @staticmethod
    def _next_expo_id(conn) -> str:
        nxt = int(db.get_meta(conn, "expo_counter", "0")) + 1
        db.set_meta(conn, "expo_counter", str(nxt))
        return f"E{nxt}"

    @staticmethod
    def _same_entries(a: list[dict], b: list[dict]) -> bool:
        return all(
            x["day"] == y["day"] and x["copy"] == y["copy"]
            and x["light"] == y["light"]
            for x, y in zip(a, b)
        ) and len(a) == len(b)

    def _schedule_among(self, entries, schedules) -> bool:
        return any(
            self._same_entries(entries, s["entries"]) for s in schedules
        )

    def _reject(self, op_id: str, fp: str, rej: Rejection) -> dict:
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            db.record_operation(conn, op_id, "register_exhibition", fp)
            result = {"status": "rejected", "error": rej.as_dict()}
            db.add_receipt(
                conn, "register_exhibition", "rejected", result, op_id
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return result
