"""Exact exhibition-track planning for identified dominoes and named bridges.

Real dominoes are physical edges between two mark vertices.  They may be turned
around, but their physical ids and multiplicities are retained: two same-marked
tiles are still two different edges.  A bridge is a named edge with fixed ends;
if chosen, it must actually be placed, and each bridge may be chosen at most
once.

The solver is exhaustive rather than a heuristic.  At most 15 real tiles and 5
bridges are accepted, so bit-mask state plus branch-and-bound is small enough,
while still handling disconnected components, odd degree parity, doubles and
forbidden adjacency exactly.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass

from .restoration import TileReconstruction

MAX_REAL_TILES = 15
MAX_BRIDGES = 5


@dataclass(frozen=True)
class TileEdge:
    """A unique physical domino whose unordered end pair is identified."""

    tile_id: str
    left_mark: str
    right_mark: str
    rivet_orientation_determined: bool = False
    possible_directed_pairs: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Bridge:
    bridge_id: str
    left_mark: str
    right_mark: str


@dataclass(frozen=True)
class Placement:
    edge_id: str
    edge_kind: str  # "real" | "bridge"
    head_mark: str
    tail_mark: str
    orientation: str  # "forward" | "reversed"
    rivet_side: str | None = None

    @property
    def directed_pair(self) -> tuple[str, str]:
        return self.head_mark, self.tail_mark

    def to_json(self) -> dict:
        return {
            "edge_id": self.edge_id,
            "edge_kind": self.edge_kind,
            "head_mark": self.head_mark,
            "tail_mark": self.tail_mark,
            "orientation": self.orientation,
            "rivet_side": self.rivet_side,
        }


@dataclass(frozen=True)
class Track:
    index: int
    placements: tuple[Placement, ...]

    def to_json(self) -> dict:
        return {"index": self.index, "placements": [p.to_json() for p in self.placements]}


@dataclass(frozen=True)
class LayoutResult:
    status: str  # "OPTIMAL" | "INFEASIBLE" | "INVALID"
    tracks: tuple[Track, ...] = ()
    borrowed_bridges: tuple[str, ...] = ()
    track_count: int = 0
    errors: tuple[str, ...] = ()
    lower_bound_tracks: int = 0

    def to_json(self) -> dict:
        return {
            "status": self.status,
            "track_count": self.track_count,
            "borrowed_bridges": list(self.borrowed_bridges),
            "lower_bound_tracks": self.lower_bound_tracks,
            "tracks": [track.to_json() for track in self.tracks],
            "errors": list(self.errors),
        }


@dataclass(frozen=True)
class _SolverEdge:
    edge_id: str
    edge_kind: str
    left_mark: str
    right_mark: str
    rivet_orientation_determined: bool
    possible_directed_pairs: frozenset[tuple[str, str]]


@dataclass
class _TrackPlanner:
    edges: tuple[_SolverEdge, ...]
    forbidden: frozenset[frozenset[str]]

    def __post_init__(self) -> None:
        self.orientations: list[tuple[str, str, str, str | None]] = [
            self._orientations(edge) for edge in self.edges
        ]
        # Memo entries belong to one top-level exact query.  A failed
        # low-track attempt must not poison a later query with more tracks.
        self._cache_token = 0
        self._cover_memo: dict[
            tuple[int, int],
            tuple[int, tuple[list[list[tuple[int, int]]], ...] | None],
        ] = {}
        self._growth_memo: set[tuple] = set()

    def _orientations(
        self, edge: _SolverEdge
    ) -> tuple[tuple[str, str, str, str | None], ...]:
        """Return the two ways a physical edge may be put into a track.

        ``possible_directed_pairs`` records where the rivet sits, not whether
        the tile is allowed to be turned.  An exhibition tile may always be
        reversed; the rivet side is derived from the physical directed pair
        that produces the requested reader-facing head/tail order.
        """

        forward = (edge.left_mark, edge.right_mark)
        reverse = (edge.right_mark, edge.left_mark)
        if edge.edge_kind == "bridge" or not edge.rivet_orientation_determined:
            return (
                (*forward, "forward", None),
                (*reverse, "reversed", None),
            )

        possibilities = set(edge.possible_directed_pairs)
        if not possibilities:
            return (
                (*forward, "forward", None),
                (*reverse, "reversed", None),
            )
        forward_rivet = "left" if forward in possibilities else "right"
        reverse_rivet = "left" if reverse in possibilities else "right"
        return (
            (*forward, "forward", forward_rivet),
            (*reverse, "reversed", reverse_rivet),
        )

    def _is_forbidden(self, first_id: str, second_id: str) -> bool:
        return frozenset((first_id, second_id)) in self.forbidden

    def lower_bound(self, mask: int) -> int:
        """Graph-theoretic lower bound: components + odd-vertex correction."""

        if mask == 0:
            return 0
        selected = [i for i, edge in enumerate(self.edges) if mask & (1 << i)]
        used_vertices = set()
        for i in selected:
            edge = self.edges[i]
            used_vertices.add(edge.left_mark)
            used_vertices.add(edge.right_mark)
        parent = {vertex: vertex for vertex in used_vertices}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        degree: dict[str, int] = {}
        vertices_seen: set[str] = set()
        for i in selected:
            edge = self.edges[i]
            vertices_seen.update((edge.left_mark, edge.right_mark))
            degree[edge.left_mark] = degree.get(edge.left_mark, 0) + 1
            degree[edge.right_mark] = degree.get(edge.right_mark, 0) + 1
            # A double does not join two distinct mark vertices.
            if edge.left_mark != edge.right_mark:
                union(edge.left_mark, edge.right_mark)

        components = len({find(v) for v in vertices_seen})
        # A connected multigraph can be covered by max(1, odd_vertices / 2)
        # edge-disjoint open/closed trails.  Doubles occupy their own vertex.
        component_vertices: dict[str, set[str]] = {}
        for vertex in vertices_seen:
            component_vertices.setdefault(find(vertex), set()).add(vertex)
        component_odds: dict[str, int] = {root: 0 for root in component_vertices}
        for vertex, count in degree.items():
            if count % 2:
                component_odds[find(vertex)] += 1
        return sum(max(1, component_odds[root] // 2) for root in component_vertices)

    def feasible(self, mask: int, max_tracks: int) -> bool:
        """Return whether all selected edges can be arranged in <= max_tracks."""

        return self.cover(mask, max_tracks) is not None

    def cover(
        self, mask: int, max_tracks: int
    ) -> tuple[list[list[tuple[int, int]]], ...] | None:
        """Find a partition into at most ``max_tracks`` directed edge sequences.

        The lowest unused edge is forced into the next track.  That track is
        then grown from either end, so the anchor need not be a trail endpoint;
        this still removes equivalent whole-track permutations from the search.
        """

        self._cache_token += 1
        if mask == 0:
            return ()
        if max_tracks <= 0 or self.lower_bound(mask) > max_tracks:
            return None
        return self._cover(mask, max_tracks)

    def _cover(
        self, mask: int, max_tracks: int
    ) -> tuple[list[list[tuple[int, int]]], ...] | None:
        key = (mask, max_tracks)
        cached = self._cover_memo.get(key)
        if cached is not None and cached[0] == self._cache_token:
            return cached[1]
        if mask == 0:
            result: tuple[list[list[tuple[int, int]]], ...] | None = ()
        elif max_tracks <= 0 or self.lower_bound(mask) > max_tracks:
            result = None
        else:
            anchor = (mask & -mask).bit_length() - 1
            result = None
            seen_directions: set[tuple[str, str]] = set()
            for orientation_index, orientation in enumerate(self.orientations[anchor]):
                head, tail, _direction, _rivet = orientation
                if (head, tail) in seen_directions:
                    continue
                seen_directions.add((head, tail))
                track = self._grow_track(
                    full_mask=mask,
                    used_mask=1 << anchor,
                    sequence=[(anchor, orientation_index)],
                    left_mark=head,
                    right_mark=tail,
                    left_id=self.edges[anchor].edge_id,
                    right_id=self.edges[anchor].edge_id,
                    tracks_left=max_tracks,
                )
                if track is not None:
                    used = {edge_index for edge_index, _orientation in track}
                    remaining = mask
                    for edge_index in used:
                        remaining ^= 1 << edge_index
                    rest = self._cover(remaining, max_tracks - 1) if remaining else ()
                    if rest is not None:
                        result = (track, *rest)
                        break
        self._cover_memo[key] = (self._cache_token, result)
        return result

    def _grow_track(
        self,
        *,
        full_mask: int,
        used_mask: int,
        sequence: list[tuple[int, int]],
        left_mark: str,
        right_mark: str,
        left_id: str,
        right_id: str,
        tracks_left: int,
    ) -> list[tuple[int, int]] | None:
        """Grow one trail in both directions; close it whenever the rest fits."""

        remaining_mask = full_mask ^ used_mask
        memo_key = (
            self._cache_token,
            full_mask,
            used_mask,
            left_mark,
            right_mark,
            left_id,
            right_id,
            tracks_left,
        )
        if memo_key in self._growth_memo:
            return None

        # Close the current trail and allocate the remaining edges to others.
        if remaining_mask == 0 or self.lower_bound(remaining_mask) <= tracks_left - 1:
            rest = self._cover(remaining_mask, tracks_left - 1) if remaining_mask else ()
            if rest is not None:
                return list(sequence)

        for next_index in self._set_bits(remaining_mask):
            next_edge = self.edges[next_index]
            seen_directions: set[tuple[str, str]] = set()
            for orientation_index, orientation in enumerate(self.orientations[next_index]):
                head, tail, _direction, _rivet = orientation
                if (head, tail) in seen_directions:
                    continue
                seen_directions.add((head, tail))

                # Prepend this edge to the trail: its tail joins the old head.
                if tail == left_mark and not self._is_forbidden(next_edge.edge_id, left_id):
                    answer = self._grow_track(
                        full_mask=full_mask,
                        used_mask=used_mask | (1 << next_index),
                        sequence=[(next_index, orientation_index), *sequence],
                        left_mark=head,
                        right_mark=right_mark,
                        left_id=next_edge.edge_id,
                        right_id=right_id,
                        tracks_left=tracks_left,
                    )
                    if answer is not None:
                        return answer

                # Append this edge: its head joins the old tail.
                if head == right_mark and not self._is_forbidden(right_id, next_edge.edge_id):
                    answer = self._grow_track(
                        full_mask=full_mask,
                        used_mask=used_mask | (1 << next_index),
                        sequence=[*sequence, (next_index, orientation_index)],
                        left_mark=left_mark,
                        right_mark=tail,
                        left_id=left_id,
                        right_id=next_edge.edge_id,
                        tracks_left=tracks_left,
                    )
                    if answer is not None:
                        return answer

        self._growth_memo.add(memo_key)
        return None

    @staticmethod
    def _set_bits(mask: int) -> list[int]:
        bits: list[int] = []
        current = mask
        low = current & -current
        while low:
            bits.append(low.bit_length() - 1)
            current ^= low
            low = current & -current
        return bits

    def build_tracks(
        self, partition: tuple[list[list[tuple[int, int]]], ...]
    ) -> tuple[Track, ...]:
        tracks: list[Track] = []
        for track_index, encoded_track in enumerate(partition):
            placements: list[Placement] = []
            for edge_index, orientation_index in encoded_track:
                edge = self.edges[edge_index]
                head, tail, direction, rivet = self.orientations[edge_index][
                    orientation_index
                ]
                placements.append(
                    Placement(
                        edge_id=edge.edge_id,
                        edge_kind=edge.edge_kind,
                        head_mark=head,
                        tail_mark=tail,
                        orientation=direction,
                        rivet_side=rivet,
                    )
                )
            self._assert_valid_track(placements)
            tracks.append(Track(index=track_index, placements=tuple(placements)))
        return tuple(tracks)

    def _assert_valid_track(self, placements: list[Placement]) -> None:
        """Defensive invariant check for a generated exhibition track."""

        for previous, current in zip(placements, placements[1:]):
            if previous.tail_mark != current.head_mark:
                raise RuntimeError("generated track has mismatched adjacent marks")
            if self._is_forbidden(previous.edge_id, current.edge_id):
                raise RuntimeError("generated track violates a forbidden adjacency")


def tile_edges_from_reconstructions(
    reconstructions: dict[str, TileReconstruction],
) -> tuple[TileEdge, ...]:
    """Materialize only the tiles whose unordered end pair is uniquely known."""

    edges: list[TileEdge] = []
    for tile_id in sorted(reconstructions):
        reconstruction = reconstructions[tile_id]
        if reconstruction.empty:
            raise ValueError(
                f"tile {tile_id!r} has no compatible directed end pair; keep it for review"
            )
        if not reconstruction.exhibit_ready:
            raise ValueError(
                f"tile {tile_id!r} still has {len(reconstruction.unordered_pairs)} "
                f"possible unordered end pairs; it cannot be placed"
            )
        pair = reconstruction.unique_unordered_pair
        assert pair is not None
        edges.append(
            TileEdge(
                tile_id=tile_id,
                left_mark=pair[0],
                right_mark=pair[1],
                rivet_orientation_determined=reconstruction.rivet_orientation_determined,
                possible_directed_pairs=reconstruction.directed_pairs,
            )
        )
    return tuple(edges)


def _validate_inputs(
    real_tiles: tuple[TileEdge, ...],
    bridges: tuple[Bridge, ...],
    forbidden_adjacent: frozenset[frozenset[str]],
) -> tuple[str, ...]:
    errors: list[str] = []
    if not isinstance(real_tiles, (tuple, list)) or not all(
        isinstance(tile, TileEdge) for tile in real_tiles
    ):
        errors.append("real_tiles must be a tuple of TileEdge objects")
        return tuple(errors)
    if not isinstance(bridges, (tuple, list)) or not all(
        isinstance(bridge, Bridge) for bridge in bridges
    ):
        errors.append("bridges must be a tuple of Bridge objects")
        return tuple(errors)

    if len(real_tiles) > MAX_REAL_TILES:
        errors.append(f"at most {MAX_REAL_TILES} real tiles are supported")
    if len(bridges) > MAX_BRIDGES:
        errors.append(f"at most {MAX_BRIDGES} bridges are supported")
    real_ids = [tile.tile_id for tile in real_tiles]
    bridge_ids = [bridge.bridge_id for bridge in bridges]
    if len(set(real_ids)) != len(real_ids):
        errors.append("real tile ids must be unique; same-marked tiles are not interchangeable")
    if len(set(bridge_ids)) != len(bridge_ids):
        errors.append("bridge ids must be unique")
    if set(real_ids) & set(bridge_ids):
        errors.append("bridge ids and real tile ids must share no names")
    for tile in real_tiles:
        if not tile.left_mark or not tile.right_mark:
            errors.append(f"tile {tile.tile_id!r}: both marks must be non-empty strings")
        for pair in tile.possible_directed_pairs:
            if not (
                isinstance(pair, (tuple, list))
                and len(pair) == 2
                and pair in ((tile.left_mark, tile.right_mark), (tile.right_mark, tile.left_mark))
            ):
                errors.append(
                    f"tile {tile.tile_id!r}: directed pair {pair!r} does not match its end marks"
                )
    for bridge in bridges:
        if not bridge.left_mark or not bridge.right_mark:
            errors.append(f"bridge {bridge.bridge_id!r}: both marks must be non-empty strings")
    known = set(real_ids) | set(bridge_ids)
    for pair in forbidden_adjacent:
        if not isinstance(pair, frozenset) or len(pair) != 2:
            errors.append("each forbidden adjacency must be a pair of two distinct named edges")
            continue
        unknown = sorted(pair - known)
        if unknown:
            errors.append(f"forbidden adjacency names unknown edges: {unknown}")
    return tuple(errors)


def _canonical_forbidden(
    raw_pairs: set[frozenset[str]] | frozenset[frozenset[str]] | None,
) -> frozenset[frozenset[str]]:
    return frozenset(raw_pairs or set())


def _make_edges(
    real_tiles: tuple[TileEdge, ...], chosen_bridges: tuple[Bridge, ...]
) -> tuple[_SolverEdge, ...]:
    edges: list[_SolverEdge] = []
    for tile in real_tiles:
        edges.append(
            _SolverEdge(
                edge_id=tile.tile_id,
                edge_kind="real",
                left_mark=tile.left_mark,
                right_mark=tile.right_mark,
                rivet_orientation_determined=tile.rivet_orientation_determined,
                possible_directed_pairs=frozenset(tile.possible_directed_pairs),
            )
        )
    for bridge in chosen_bridges:
        edges.append(
            _SolverEdge(
                edge_id=bridge.bridge_id,
                edge_kind="bridge",
                left_mark=bridge.left_mark,
                right_mark=bridge.right_mark,
                rivet_orientation_determined=False,
                possible_directed_pairs=frozenset(),
            )
        )
    return tuple(edges)


def _infeasible(
    real_tiles: tuple[TileEdge, ...],
    bridges: tuple[Bridge, ...],
    forbidden: frozenset[frozenset[str]],
    errors: tuple[str, ...] = (),
) -> LayoutResult:
    valid_real = tuple(tile for tile in real_tiles if isinstance(tile, TileEdge))
    planner = _TrackPlanner(_make_edges(valid_real, ()), forbidden)
    return LayoutResult(
        status="INVALID" if errors else "INFEASIBLE",
        errors=errors,
        lower_bound_tracks=planner.lower_bound((1 << len(valid_real)) - 1),
    )


def minimize_tracks(
    real_tiles: tuple[TileEdge, ...],
    bridges: tuple[Bridge, ...],
    *,
    max_borrowed_bridges: int,
    forbidden_adjacent: set[frozenset[str]] | None = None,
) -> LayoutResult:
    """Minimize tracks; among equal track counts use the fewest borrowed bridges."""

    forbidden = _canonical_forbidden(forbidden_adjacent)
    errors = _validate_inputs(real_tiles, bridges, forbidden)
    if type(max_borrowed_bridges) is not int or not 0 <= max_borrowed_bridges <= len(bridges):
        errors = (*errors, "max_borrowed_bridges must be between 0 and the number of bridges")
    if errors:
        return _infeasible(real_tiles, bridges, forbidden, errors)

    all_mask = (1 << len(real_tiles)) - 1
    best: LayoutResult | None = None
    # Bridge count is the tie-breaker, so enumerate subsets in increasing size.
    for borrow_count in range(max_borrowed_bridges + 1):
        for chosen_indices in itertools.combinations(range(len(bridges)), borrow_count):
            chosen = tuple(bridges[i] for i in chosen_indices)
            edges = _make_edges(real_tiles, chosen)
            planner = _TrackPlanner(edges, forbidden)
            mask = (1 << len(edges)) - 1
            lower = planner.lower_bound(mask)
            if best is not None and lower >= best.track_count:
                continue
            # Exact search starts at the graph lower bound; forbidden pairs may
            # force one additional track even when parity/connectivity match.
            for track_count in range(lower, len(edges) + 1):
                if best is not None and track_count >= best.track_count:
                    break
                partition = planner.cover(mask, track_count)
                if partition is not None:
                    candidate = LayoutResult(
                        status="OPTIMAL",
                        tracks=planner.build_tracks(partition),
                        borrowed_bridges=tuple(bridge.bridge_id for bridge in chosen),
                        track_count=len(partition),
                        lower_bound_tracks=lower,
                    )
                    if best is None or (
                        candidate.track_count, len(candidate.borrowed_bridges)
                    ) < (best.track_count, len(best.borrowed_bridges)):
                        best = candidate
                    break
    if best is None:
        return _infeasible(real_tiles, bridges, forbidden)
    return best


def minimum_bridges_for_tracks(
    real_tiles: tuple[TileEdge, ...],
    bridges: tuple[Bridge, ...],
    *,
    available_tracks: int,
    forbidden_adjacent: set[frozenset[str]] | None = None,
) -> LayoutResult:
    """With a fixed track budget, minimize the number of borrowed bridges."""

    forbidden = _canonical_forbidden(forbidden_adjacent)
    errors = _validate_inputs(real_tiles, bridges, forbidden)
    if type(available_tracks) is not int or available_tracks < 1:
        errors = (*errors, "available_tracks must be a positive integer")
    if errors:
        return _infeasible(real_tiles, bridges, forbidden, errors)

    for borrow_count in range(len(bridges) + 1):
        for chosen_indices in itertools.combinations(range(len(bridges)), borrow_count):
            chosen = tuple(bridges[i] for i in chosen_indices)
            edges = _make_edges(real_tiles, chosen)
            planner = _TrackPlanner(edges, forbidden)
            mask = (1 << len(edges)) - 1
            if planner.feasible(mask, available_tracks):
                partition = planner.cover(mask, available_tracks)
                assert partition is not None
                return LayoutResult(
                    status="OPTIMAL",
                    tracks=planner.build_tracks(partition),
                    borrowed_bridges=tuple(bridge.bridge_id for bridge in chosen),
                    track_count=len(partition),
                    lower_bound_tracks=planner.lower_bound(mask),
                )
    return _infeasible(real_tiles, bridges, forbidden)
