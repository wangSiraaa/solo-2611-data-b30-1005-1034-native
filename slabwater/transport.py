"""Minimum-cost sand transfer over a directed cart-road network.

Input is the ``old - aligned_target`` difference map: positive cells have
scoopable sand, negative cells need fill.  The workshop owns a directed road
network; every lane has an integer batch capacity (in spoons) and a per-spoon
freight cost, and sand may be transshipped at intermediate cells.  A genuinely
surplus cell may also send excess away at a unit price, and a genuinely short
cell may buy in at a unit price.

The problem is a min-cost transshipment (a single commodity, so two shipments
over the same lane automatically share its capacity).  We reduce it to a
balanced min-cost flow by adding a reservoir node that absorbs the net surplus
or supplies the net deficit; costs are non-negative, so an optimal solution
contains no free circulation and no closed loop can invent sand.

All quantities are integers.  Divisible sand is allowed, but because the data
is integral the network LP has an integral optimum, so the reported spoon
quantities are exact.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .model import Cell, is_int

_INF = 2**62


class TransportError(ValueError):
    def __init__(self, code: str, message: str, details: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


def _parse_cell(raw: Any, what: str) -> Cell:
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        raise TransportError("bad_cell", f"{what} must be a [row, col] pair")
    r, c = raw
    if not is_int(r) or not is_int(c):
        raise TransportError("bad_cell", f"{what} coordinates must be integers")
    return int(r), int(c)


@dataclass(frozen=True)
class Lane:
    """One directed cart lane with a shared integer capacity and unit cost."""

    source: Cell
    target: Cell
    capacity: int
    cost: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "from": [self.source[0], self.source[1]],
            "to": [self.target[0], self.target[1]],
            "capacity": self.capacity,
            "cost": self.cost,
        }


@dataclass(frozen=True)
class LaneFlow:
    source: Cell
    target: Cell
    capacity: int
    cost: int
    flow: int

    @property
    def freight(self) -> int:
        return self.flow * self.cost

    def to_dict(self) -> dict[str, Any]:
        return {
            "from": [self.source[0], self.source[1]],
            "to": [self.target[0], self.target[1]],
            "capacity": self.capacity,
            "cost": self.cost,
            "flow": self.flow,
            "freight": self.freight,
            "remaining_capacity": self.capacity - self.flow,
        }


@dataclass(frozen=True)
class CellBalance:
    cell: Cell
    delta: int
    shipped_out: int
    shipped_in: int
    outsourced: int
    bought: int
    residual: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "cell": [self.cell[0], self.cell[1]],
            "delta": self.delta,
            "scoopable": self.delta if self.delta > 0 else 0,
            "fill_needed": -self.delta if self.delta < 0 else 0,
            "shipped_out": self.shipped_out,
            "shipped_in": self.shipped_in,
            "outsourced": self.outsourced,
            "bought": self.bought,
            "residual": self.residual,
            "conserved": self.residual == 0,
        }


@dataclass(frozen=True)
class TransportResult:
    ok: bool
    total_cost: int
    freight_cost: int
    outsource_cost: int
    buy_cost: int
    lane_total: int
    moved_total: int
    outsourced_total: int
    bought_total: int
    lanes: tuple[LaneFlow, ...]
    balances: tuple[CellBalance, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "total_cost": self.total_cost,
            "freight_cost": self.freight_cost,
            "outsource_cost": self.outsource_cost,
            "buy_cost": self.buy_cost,
            "lane_total": self.lane_total,
            "moved_total": self.moved_total,
            "outsourced_total": self.outsourced_total,
            "bought_total": self.bought_total,
            "lanes": [lane.to_dict() for lane in self.lanes],
            "balances": [balance.to_dict() for balance in self.balances],
        }


def parse_lanes(raw_lanes: Sequence[Any], height: int, width: int) -> tuple[Lane, ...]:
    """Validate lanes and reject conflicting duplicates of one directed lane."""

    if not isinstance(raw_lanes, (list, tuple)):
        raise TransportError("bad_lanes", "lanes must be a list")
    by_pair: dict[tuple[Cell, Cell], Lane] = {}
    for index, raw in enumerate(raw_lanes):
        if not isinstance(raw, Mapping):
            raise TransportError("bad_lanes", f"lane[{index}] must be an object")
        source = _parse_cell(raw.get("from"), f"lane[{index}].from")
        target = _parse_cell(raw.get("to"), f"lane[{index}].to")
        capacity = raw.get("capacity")
        cost = raw.get("cost")
        for label, value in (("capacity", capacity), ("cost", cost)):
            if not is_int(value) or value < 0:
                raise TransportError(
                    "bad_lanes", f"lane[{index}] {label} must be a non-negative integer"
                )
        for cell in (source, target):
            if not (0 <= cell[0] < height and 0 <= cell[1] < width):
                raise TransportError(
                    "lane_out_of_bounds",
                    f"lane[{index}] endpoint {list(cell)} is outside the {height}x{width} plate",
                )
        if source == target:
            continue  # a self lane moves nothing; it cannot help and is dropped
        lane = Lane(source, target, int(capacity), int(cost))
        key = (source, target)
        previous = by_pair.get(key)
        if previous is None:
            by_pair[key] = lane
        elif (previous.capacity, previous.cost) != (lane.capacity, lane.cost):
            raise TransportError(
                "conflicting_lane",
                f"directed lane {list(source)}->{list(target)} is declared twice with "
                "different capacity/cost; one lane must share one capacity",
                {"lane": [list(source), list(target)]},
            )
        # identical duplicates are one lane, not double capacity
    return tuple(by_pair[key] for key in sorted(by_pair))


def _prices(
    height: int,
    width: int,
    default: int | None,
    overrides: Mapping[str, Any] | Mapping[Cell, Any] | None,
    label: str,
) -> dict[Cell, int]:
    if default is not None and (not is_int(default) or default < 0):
        raise TransportError("bad_price", f"{label} price must be a non-negative integer")
    prices: dict[Cell, int] = {}
    if default is not None:
        for r in range(height):
            for c in range(width):
                prices[(r, c)] = int(default)
    if overrides:
        if not isinstance(overrides, Mapping):
            raise TransportError("bad_price", f"{label} prices must be an object")
        for key, value in overrides.items():
            if isinstance(key, str):
                try:
                    r_str, c_str = key.split(",")
                    cell = (int(r_str), int(c_str))
                except (ValueError, AttributeError):
                    raise TransportError("bad_price", f"{label} price key {key!r} is not 'row,col'")
            elif isinstance(key, (list, tuple)) and len(key) == 2:
                cell = _parse_cell(key, f"{label} price key")
            else:
                raise TransportError("bad_price", f"{label} price key {key!r} is invalid")
            if not (0 <= cell[0] < height and 0 <= cell[1] < width):
                raise TransportError(
                    "price_out_of_bounds", f"{label} price cell {list(cell)} is outside the plate"
                )
            if not is_int(value) or value < 0:
                raise TransportError(
                    "bad_price", f"{label} price at {list(cell)} must be a non-negative integer"
                )
            prices[cell] = int(value)
    return prices


class _MinCostFlow:
    def __init__(self, nodes: int):
        self.nodes = nodes
        self.graph: list[list[list[int]]] = [[] for _ in range(nodes)]

    def add_edge(self, u: int, v: int, capacity: int, cost: int) -> list[int]:
        """Add a directed edge and return the forward edge object."""

        forward = [v, capacity, cost, len(self.graph[v])]
        backward = [u, 0, -cost, len(self.graph[u])]
        self.graph[u].append(forward)
        self.graph[v].append(backward)
        return forward

    def flow(self, source: int, sink: int, required: int) -> tuple[int, int]:
        """Send up to ``required`` units; return (sent, cost)."""

        n = self.nodes
        potential = [0] * n
        sent = 0
        cost = 0
        while sent < required:
            dist = [_INF] * n
            prev_node = [-1] * n
            prev_edge = [-1] * n
            dist[source] = 0
            heap: list[tuple[int, int]] = [(0, source)]
            while heap:
                d, node = heapq.heappop(heap)
                if d > dist[node]:
                    continue
                for index, edge in enumerate(self.graph[node]):
                    to, capacity, edge_cost, _ = edge
                    if capacity <= 0:
                        continue
                    reduced = edge_cost + potential[node] - potential[to]
                    nd = d + reduced
                    if nd < dist[to]:
                        dist[to] = nd
                        prev_node[to] = node
                        prev_edge[to] = index
                        heapq.heappush(heap, (nd, to))
            if dist[sink] == _INF:
                break
            for node in range(n):
                if dist[node] < _INF:
                    potential[node] += dist[node]
            augment = required - sent
            node = sink
            while node != source:
                edge = self.graph[prev_node[node]][prev_edge[node]]
                augment = min(augment, edge[1])
                node = prev_node[node]
            node = sink
            while node != source:
                edge = self.graph[prev_node[node]][prev_edge[node]]
                edge[1] -= augment
                self.graph[node][edge[3]][1] += augment
                node = prev_node[node]
            sent += augment
            # potential[sink] is the actual shortest-path cost with potential[source]==0
            cost += augment * potential[sink]
        return sent, cost


def plan_transport(
    delta: Sequence[Sequence[int]],
    lanes: Sequence[Any],
    outsource_price: int | None = None,
    buy_price: int | None = None,
    outsource_prices: Mapping[Any, Any] | None = None,
    buy_prices: Mapping[Any, Any] | None = None,
) -> TransportResult:
    """Find the minimum-cost way to clear a difference map over the road net."""

    height = len(delta)
    width = len(delta[0]) if height else 0
    for r, row in enumerate(delta):
        if len(row) != width:
            raise TransportError("ragged_delta", "delta matrix is not rectangular")
        for value in row:
            if not is_int(value):
                raise TransportError("bad_delta", "delta entries must be integers")
    parsed_lanes = parse_lanes(lanes, height, width)
    out_prices = _prices(height, width, outsource_price, outsource_prices, "outsource")
    in_prices = _prices(height, width, buy_price, buy_prices, "buy")

    cells: list[Cell] = [(r, c) for r in range(height) for c in range(width)]
    index = {cell: i for i, cell in enumerate(cells)}
    ground = len(cells)
    source = ground + 1
    sink = ground + 2
    mcf = _MinCostFlow(ground + 3)

    # Net supply per node: b>0 must leave, b<0 must arrive.  The ground node
    # carries the mirror-image imbalance so b sums to zero.
    supply: dict[int, int] = {}
    total_delta = 0
    for cell in cells:
        value = delta[cell[0]][cell[1]]
        supply[index[cell]] = value
        total_delta += value
    supply[ground] = -total_delta

    lane_edges: list[tuple[Lane, list[int]]] = []
    for lane in parsed_lanes:
        edge = mcf.add_edge(index[lane.source], index[lane.target], lane.capacity, lane.cost)
        lane_edges.append((lane, edge))

    # Outsource only leaves a true surplus cell; buy only enters a true deficit.
    out_edges: dict[Cell, list[int]] = {}
    buy_edges: dict[Cell, list[int]] = {}
    for cell in cells:
        amount = delta[cell[0]][cell[1]]
        if amount > 0:
            price = out_prices.get(cell)
            if price is not None:
                out_edges[cell] = mcf.add_edge(
                    index[cell], ground, amount, price
                )
        elif amount < 0:
            price = in_prices.get(cell)
            if price is not None:
                buy_edges[cell] = mcf.add_edge(
                    ground, index[cell], -amount, price
                )

    required = 0
    for node_id, value in supply.items():
        if value > 0:
            required += value
            mcf.add_edge(source, node_id, value, 0)
        elif value < 0:
            mcf.add_edge(node_id, sink, -value, 0)

    sent, flow_cost = mcf.flow(source, sink, required)
    if sent != required:
        raise TransportError(
            "transport_infeasible",
            f"could only route {sent} of {required} required spoons",
            {"required": required, "routed": sent},
        )

    lane_flows: list[LaneFlow] = []
    shipped_out: dict[Cell, int] = {cell: 0 for cell in cells}
    shipped_in: dict[Cell, int] = {cell: 0 for cell in cells}
    freight_cost = 0
    shipped_total = 0
    for lane, edge in lane_edges:
        used = lane.capacity - edge[1]
        lane_flows.append(
            LaneFlow(lane.source, lane.target, lane.capacity, lane.cost, used)
        )
        shipped_out[lane.source] += used
        shipped_in[lane.target] += used
        freight_cost += used * lane.cost
        shipped_total += used

    outsourced: dict[Cell, int] = {cell: 0 for cell in cells}
    bought: dict[Cell, int] = {cell: 0 for cell in cells}
    outsource_cost = 0
    buy_cost = 0
    for cell, edge in out_edges.items():
        used = delta[cell[0]][cell[1]] - edge[1]
        outsourced[cell] = used
        outsource_cost += used * out_prices[cell]
    for cell, edge in buy_edges.items():
        used = (-delta[cell[0]][cell[1]]) - edge[1]
        bought[cell] = used
        buy_cost += used * in_prices[cell]

    cell_balances: list[CellBalance] = []
    moved_total = 0
    for cell in cells:
        amount = delta[cell[0]][cell[1]]
        residual = (
            amount
            + shipped_in[cell]
            + bought[cell]
            - shipped_out[cell]
            - outsourced[cell]
        )
        if amount < 0:
            # Spoons actually delivered into a short cell along the carts.
            moved_total += shipped_in[cell]
        cell_balances.append(
            CellBalance(
                cell=cell,
                delta=amount,
                shipped_out=shipped_out[cell],
                shipped_in=shipped_in[cell],
                outsourced=outsourced[cell],
                bought=bought[cell],
                residual=residual,
            )
        )
        if residual != 0:
            raise TransportError(
                "conservation_failed",
                f"cell {list(cell)} does not conserve sand",
                {"cell": list(cell), "residual": residual},
            )

    total_cost = freight_cost + outsource_cost + buy_cost
    if total_cost != flow_cost:
        # The flow cost also includes reservoir edges; they are exactly the
        # outsource/buy costs, so the two accounts must agree.
        raise TransportError(
            "cost_mismatch",
            "internally inconsistent transport cost",
            {"flow_cost": flow_cost, "accounted": total_cost},
        )

    return TransportResult(
        ok=True,
        total_cost=total_cost,
        freight_cost=freight_cost,
        outsource_cost=outsource_cost,
        buy_cost=buy_cost,
        lane_total=shipped_total,
        moved_total=moved_total,
        outsourced_total=sum(outsourced.values()),
        bought_total=sum(bought.values()),
        lanes=tuple(lane_flows),
        balances=tuple(cell_balances),
    )
