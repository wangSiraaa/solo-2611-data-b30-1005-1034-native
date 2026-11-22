"""Independent elevation catalog and opened-outlet storage plan verifier.

Synthetic discrete model only -- not for field hydraulic decisions.
"""

from .catalog import Catalog
from .lakes import (
    Block,
    DepthProduct,
    Eligible,
    Ladder,
    LakeError,
    LostName,
    MigrationResult,
    NameTag,
    OldName,
    build_ladder,
    components_at,
    depth_product_from_grid,
    depth_product_from_plan,
    eligible_blocks,
    kinship_report,
    ladder_report,
    migrate,
)
from .model import (
    BatchError,
    BatchResult,
    Cell,
    Outlet,
    Slice,
    SourceRef,
    TileSnapshot,
    WaterResult,
    canonical_json,
    sha256_hex,
)
from .publisher import PublishError, PublishResult, Publisher, VerifyResult
from .solver import BottleneckCache, SolveResult, SolverStats, enumerate_subsets, solve

__all__ = [
    "BatchError",
    "BatchResult",
    "Block",
    "BottleneckCache",
    "Catalog",
    "Cell",
    "DepthProduct",
    "Eligible",
    "Ladder",
    "LakeError",
    "LostName",
    "MigrationResult",
    "NameTag",
    "OldName",
    "Outlet",
    "PublishError",
    "PublishResult",
    "Publisher",
    "Slice",
    "SolveResult",
    "SolverStats",
    "SourceRef",
    "TileSnapshot",
    "VerifyResult",
    "WaterResult",
    "build_ladder",
    "canonical_json",
    "components_at",
    "depth_product_from_grid",
    "depth_product_from_plan",
    "eligible_blocks",
    "enumerate_subsets",
    "kinship_report",
    "ladder_report",
    "migrate",
    "sha256_hex",
    "solve",
]
