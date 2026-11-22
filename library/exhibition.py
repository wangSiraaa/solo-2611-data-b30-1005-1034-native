"""书链排展：在已有口述凭单上求“起点→终点、必经作品各一次”的最省书链。

问题约束
--------
- 书链是所列作品的一个排列：起点第一、终点最后，其余必经作品各出现一次，
  只用这些作品且各不重复（最多 9 部）。
- 每一相邻过渡必须由一名**实际读者**持有效口述凭单担任讲述；
  该凭单要给出两端真实取件（hold/copy 与原书目），讲述人及其采访场次。
- 同一位读者在整条书链中最多讲一次（哪怕他给出过两段好材料）。
- 一场采访可容纳多位真实受访人，但开场费只收一次；每场席位有限。
- 读者只可在自己签过名的场次出席。
- 目标：最小化所开采访场的开场费总和。

只在给定目录修订的**最新 epoch** 凭单上排展：补录/撤回后重新出具，
旧 epoch 凭单仍然留档但不参与排展。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import db
from .errors import Rejection

MAX_WORKS = 9
MAX_READERS = 12
MAX_SESSIONS = 6


class ExhibitionService:
    def __init__(self, conn: sqlite3.Connection, catalog: Any, orals: Any) -> None:
        self.conn = conn
        self.catalog = catalog
        self.orals = orals

    # ================================================================ 排展

    def plan_exhibition(
        self,
        op_id: str,
        plan_id: str,
        revision: str,
        start: str,
        end: str,
        via: list[str],
    ) -> dict[str, Any]:
        """求最省单条书链并落定（逐段受访人、场次与席位）。"""
        payload = {
            "plan_id": plan_id,
            "revision": revision,
            "start": start,
            "end": end,
            "via": sorted(via),
        }
        fp = db.fingerprint("plan_exhibition", payload)
        conn = self.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            prior = db.check_idempotent(conn, op_id, "plan_exhibition", fp)
            if prior is not None:
                conn.commit()
                return prior
            if conn.execute(
                "SELECT 1 FROM plans WHERE plan_id=?", (plan_id,)
            ).fetchone():
                raise Rejection("plan_exists", plan=plan_id)
            if not conn.execute(
                "SELECT 1 FROM revisions WHERE revision=?", (revision,)
            ).fetchone():
                raise Rejection("unknown_revision", revision=revision)

            nodes, err = self._validate_shape(start, end, via)
            if err is not None:
                raise err

            sessions = self._load_sessions()
            if len(sessions) > MAX_SESSIONS:
                raise Rejection(
                    "too_many_sessions_available", count=len(sessions)
                )
            signups = self._load_signups()
            vouchers = self.orals.get_vouchers(revision)  # 最新 epoch

            # 规范化端点必须都出现在该修订的规范作品里
            canon = self.catalog.canonical_map(revision)
            known = set(canon.values())
            missing = [w for w in nodes if w not in known]
            if missing:
                raise Rejection(
                    "work_not_in_revision", revision=revision, works=missing
                )

            # 有向边 -> 读者 -> 代表凭单
            edge_map: dict[tuple[str, str], dict[str, dict]] = {}
            readers_in_graph: set[str] = set()
            for v in vouchers:
                key = (v["from_canonical"], v["to_canonical"])
                readers_in_graph.add(v["reader"])
                edge_map.setdefault(key, {}).setdefault(v["reader"], v)
            if len(readers_in_graph) > MAX_READERS:
                raise Rejection(
                    "too_many_readers_available", count=len(readers_in_graph)
                )

            paths = self._hamiltonian_paths(nodes, start, end, edge_map)
            if not paths:
                raise Rejection(
                    "no_chain",
                    start=start,
                    end=end,
                    via=list(via),
                    detail="无凭单覆盖所需的全部相邻过渡",
                )

            best: dict[str, Any] | None = None
            for path in paths:
                edges = list(zip(path, path[1:]))
                sol = self._solve_assignment(edges, edge_map, sessions, signups)
                if sol is None:
                    continue
                cost, assignment = sol
                sig = tuple((a["reader"], a["session"]) for a in assignment)
                if (
                    best is None
                    or cost < best["cost"]
                    or (cost == best["cost"] and sig < best["sig"])
                ):
                    best = {
                        "cost": cost,
                        "sig": sig,
                        "path": path,
                        "assignment": assignment,
                    }
            if best is None:
                raise Rejection(
                    "no_feasible_plan",
                    start=start,
                    end=end,
                    via=list(via),
                    detail="有路径但受访人/场次/席位无法满足",
                )

            path = best["path"]
            assignment = best["assignment"]
            seats: dict[str, int] = {}
            edge_rows = []
            for idx, (a, (u, v)) in enumerate(zip(assignment, zip(path, path[1:]))):
                seat = seats.get(a["session"], 0) + 1
                seats[a["session"]] = seat
                edge_rows.append(
                    {
                        "edge_index": idx,
                        "from_work": u,
                        "to_work": v,
                        "reader": a["reader"],
                        "session": a["session"],
                        "seat": seat,
                        "voucher": a["voucher"]["voucher_id"],
                        "from_hold": a["voucher"]["from_hold"],
                        "to_hold": a["voucher"]["to_hold"],
                        "from_book": a["voucher"]["from_book"],
                        "to_book": a["voucher"]["to_book"],
                    }
                )
            opened = sorted({a["session"] for a in assignment})
            total_fee = sum(sessions[s]["fee"] for s in opened)

            ts = db.now_iso()
            conn.execute(
                "INSERT INTO plans(plan_id, revision, chain, total_fee, created_at) "
                "VALUES(?, ?, ?, ?, ?)",
                (
                    plan_id,
                    revision,
                    json.dumps(path),
                    total_fee,
                    ts,
                ),
            )
            for s in opened:
                conn.execute(
                    "INSERT INTO plan_sessions(plan_id, session_id, fee) "
                    "VALUES(?, ?, ?)",
                    (plan_id, s, sessions[s]["fee"]),
                )
            for e in edge_rows:
                conn.execute(
                    "INSERT INTO plan_edges(plan_id, edge_index, from_work, "
                    "to_work, reader_id, session_id, seat_no, voucher) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        plan_id,
                        e["edge_index"],
                        e["from_work"],
                        e["to_work"],
                        e["reader"],
                        e["session"],
                        e["seat"],
                        e["voucher"],
                    ),
                )
            result = {
                "status": "ok",
                "plan_id": plan_id,
                "revision": revision,
                "chain": path,
                "total_fee": total_fee,
                "edges": edge_rows,
                "opened_sessions": [
                    {"session_id": s, "fee": sessions[s]["fee"]} for s in opened
                ],
            }
            db.record_operation(conn, op_id, "plan_exhibition", fp)
            db.add_receipt(conn, "plan_exhibition", "ok", result, op_id)
            conn.commit()
            return result
        except Rejection as rej:
            conn.rollback()
            return self._reject("plan_exhibition", op_id, fp, rej)
        except Exception:
            conn.rollback()
            raise

    # ================================================================ 读取

    def get_plan(self, plan_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if row is None:
            raise Rejection("unknown_plan", plan=plan_id)
        edges = [
            {
                "edge_index": r["edge_index"],
                "from_work": r["from_work"],
                "to_work": r["to_work"],
                "reader": r["reader_id"],
                "session": r["session_id"],
                "seat": r["seat_no"],
                "voucher": r["voucher"],
            }
            for r in self.conn.execute(
                "SELECT * FROM plan_edges WHERE plan_id=? ORDER BY edge_index",
                (plan_id,),
            )
        ]
        sessions = [
            {"session_id": r["session_id"], "fee": r["fee"]}
            for r in self.conn.execute(
                "SELECT * FROM plan_sessions WHERE plan_id=? ORDER BY session_id",
                (plan_id,),
            )
        ]
        return {
            "plan_id": row["plan_id"],
            "revision": row["revision"],
            "chain": json.loads(row["chain"]),
            "total_fee": row["total_fee"],
            "edges": edges,
            "opened_sessions": sessions,
        }

    # ================================================================ 内部

    @staticmethod
    def _validate_shape(
        start: str, end: str, via: list[str]
    ) -> tuple[list[str], Rejection | None]:
        nodes = [start, *via, end]
        if start == end:
            return nodes, Rejection("start_equals_end", work=start)
        if len(set(nodes)) != len(nodes):
            dup = sorted({w for w in nodes if nodes.count(w) > 1})
            return nodes, Rejection("duplicate_work_in_chain", works=dup)
        if len(nodes) > MAX_WORKS:
            return nodes, Rejection("too_many_works", count=len(nodes), limit=MAX_WORKS)
        if not nodes:
            return nodes, Rejection("empty_chain")
        return nodes, None

    def _load_sessions(self) -> dict[str, dict[str, Any]]:
        return {
            r["session_id"]: {
                "capacity": r["capacity"],
                "fee": r["fee"],
            }
            for r in self.conn.execute("SELECT * FROM sessions")
        }

    def _load_signups(self) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        for r in self.conn.execute("SELECT reader_id, session_id FROM session_signups"):
            out.setdefault(r["reader_id"], set()).add(r["session_id"])
        return out

    def _hamiltonian_paths(
        self,
        nodes: list[str],
        start: str,
        end: str,
        edge_map: dict[tuple[str, str], dict[str, dict]],
    ) -> list[list[str]]:
        """枚举从 start 到 end、恰好覆盖全部节点的简单路径。

        只沿“有凭单覆盖”的有向边扩展，天然剪掉大量无凭单排列。
        """
        middles = [n for n in nodes if n not in (start, end)]
        idx = {n: i for i, n in enumerate(middles)}
        full = (1 << len(middles)) - 1
        paths: list[list[str]] = []

        def dfs(node: str, visited: int, path: list[str]) -> None:
            if node == end:
                if visited == full:
                    paths.append(list(path))
                return
            for nxt in nodes:
                if nxt in (start, end) and nxt != end:
                    continue
                if (node, nxt) not in edge_map:
                    continue
                if nxt == end:
                    if visited == full:
                        dfs(end, visited, path + [end])
                    continue
                bit = 1 << idx[nxt]
                if visited & bit:
                    continue
                dfs(nxt, visited | bit, path + [nxt])

        if start == end:
            return []
        # start 的访问位不计入 middles
        dfs(start, 0, [start])
        return paths

    def _solve_assignment(
        self,
        edges: list[tuple[str, str]],
        edge_map: dict[tuple[str, str], dict[str, dict]],
        sessions: dict[str, dict[str, Any]],
        signups: dict[str, set[str]],
    ) -> tuple[int, list[dict[str, Any]]] | None:
        """为给定路径的每条边挑一名不同读者与其签过的场次，最小化开场费。

        读者互不重复、场次席位有限；场次费只在首次使用该场时累加。
        """
        per_edge: list[list[dict[str, Any]]] = []
        for u, v in edges:
            options: list[dict[str, Any]] = []
            for reader, vch in edge_map.get((u, v), {}).items():
                for session in signups.get(reader, set()):
                    if session not in sessions:
                        continue
                    options.append(
                        {
                            "reader": reader,
                            "session": session,
                            "voucher": vch,
                            "fee": sessions[session]["fee"],
                        }
                    )
            if not options:
                return None
            # 便宜（含已开场的 0 增量）优先，利于尽早取得好解
            options.sort(key=lambda o: (o["fee"], o["reader"], o["session"]))
            per_edge.append(options)

        best: dict[str, Any] | None = None

        def dfs(i: int, cost: int, used: set[str], seats: dict[str, int],
                assign: list[dict[str, Any]]) -> None:
            nonlocal best
            if best is not None and cost > best["cost"]:
                return
            if i == len(edges):
                sig = tuple((a["reader"], a["session"]) for a in assign)
                if (
                    best is None
                    or cost < best["cost"]
                    or (cost == best["cost"] and sig < best["sig"])
                ):
                    best = {"cost": cost, "sig": sig, "assign": list(assign)}
                return
            for opt in per_edge[i]:
                reader, session = opt["reader"], opt["session"]
                if reader in used:
                    continue
                cap = sessions[session]["capacity"]
                if seats.get(session, 0) >= cap:
                    continue
                opened = session in seats
                new_cost = cost + (0 if opened else opt["fee"])
                if best is not None and new_cost > best["cost"]:
                    continue
                used.add(reader)
                seats[session] = seats.get(session, 0) + 1
                assign.append(opt)
                dfs(i + 1, new_cost, used, seats, assign)
                assign.pop()
                seats[session] -= 1
                if seats[session] == 0:
                    del seats[session]
                used.remove(reader)

        dfs(0, 0, set(), {}, [])
        if best is None:
            return None
        return best["cost"], best["assign"]

    def _reject(self, kind: str, op_id: str, fp: str, rej: Rejection) -> dict[str, Any]:
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            db.record_operation(conn, op_id, kind, fp)
            result = {"status": "rejected", "error": rej.as_dict()}
            db.add_receipt(conn, kind, "rejected", result, op_id)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return result
