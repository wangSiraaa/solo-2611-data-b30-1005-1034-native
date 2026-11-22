"""Independent elevation catalog and opened-outlet storage plan verifier.

Synthetic discrete model only -- not for field hydraulic decisions.
"""

from .align import (
    AlignmentError,
    AlignmentResult,
    DeltaCell,
    DifferenceMap,
    RigidTransform,
    align_slices,
    compute_difference,
)
from .catalog import Catalog
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
from .renovation import RenovationError, analyze, run_material, run_material_file
from .solver import BottleneckCache, SolveResult, SolverStats, enumerate_subsets, solve
from .transport import (
    CellBalance,
    Lane,
    LaneFlow,
    TransportError,
    TransportResult,
    plan_transport,
)

__all__ = [
    "AlignmentError",
    "AlignmentResult",
    "BatchError",
    "BatchResult",
    "BottleneckCache",
    "Catalog",
    "Cell",
    "CellBalance",
    "DeltaCell",
    "DifferenceMap",
    "Lane",
    "LaneFlow",
    "Outlet",
    "PublishError",
    "PublishResult",
    "Publisher",
    "RenovationError",
    "RigidTransform",
    "Slice",
    "SolveResult",
    "SolverStats",
    "SourceRef",
    "TileSnapshot",
    "TransportError",
    "TransportResult",
    "VerifyResult",
    "WaterResult",
    "align_slices",
    "analyze",
    "canonical_json",
    "compute_difference",
    "enumerate_subsets",
    "plan_transport",
    "run_material",
    "run_material_file",
    "sha256_hex",
    "solve",
]
