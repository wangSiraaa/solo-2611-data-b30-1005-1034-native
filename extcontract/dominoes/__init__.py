"""Domino rubbing restoration and exact exhibition-track planning.

The package separates three concerns in the same style as the external-contract
toolkit:

  * evidence-only rubbing ingestion with revision/atomicity rules;
  * whole-tile reconstruction using intersecting witness possibilities;
  * exact track/bridge planning that preserves physical tile identity.
"""

from .ingest import FixedRubbingContract, RubbingLibrary
from .layout import (
    Bridge,
    LayoutResult,
    Placement,
    TileEdge,
    Track,
    minimum_bridges_for_tracks,
    minimize_tracks,
    tile_edges_from_reconstructions,
)
from .model import (
    DEFAULT_READER_RIVET_SIDE,
    DominoIngestResult,
    Rubbing,
    RubbingState,
    parse_rubbing,
)
from .restoration import (
    TileReconstruction,
    WitnessPossibilities,
    possible_directed_end_pairs,
    reconstruct_all,
    reconstruct_tile,
    rubbing_possibilities,
)

__all__ = [
    "FixedRubbingContract",
    "RubbingLibrary",
    "Bridge",
    "LayoutResult",
    "Placement",
    "TileEdge",
    "Track",
    "minimum_bridges_for_tracks",
    "minimize_tracks",
    "tile_edges_from_reconstructions",
    "DEFAULT_READER_RIVET_SIDE",
    "DominoIngestResult",
    "Rubbing",
    "RubbingState",
    "parse_rubbing",
    "TileReconstruction",
    "WitnessPossibilities",
    "possible_directed_end_pairs",
    "reconstruct_all",
    "reconstruct_tile",
    "rubbing_possibilities",
]
