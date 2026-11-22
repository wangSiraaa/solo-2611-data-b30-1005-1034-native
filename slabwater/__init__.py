"""Independent elevation catalog and opened-outlet storage plan verifier.

Synthetic discrete model only -- not for field hydraulic decisions.
"""

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
from .solver import BottleneckCache, SolveResult, SolverStats, enumerate_subsets, solve
from .tray import LookupResult, Pan, PanCell, WSegment, ZSegment
from .balance import InfeasibleBalance, LoadingPlan, Weight, plan_balance

__all__ = [
    "BatchError",
    "BatchResult",
    "BottleneckCache",
    "Catalog",
    "Cell",
    "InfeasibleBalance",
    "LoadingPlan",
    "Outlet",
    "Pan",
    "PanCell",
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
    "Weight",
    "WSegment",
    "ZSegment",
    "LookupResult",
    "canonical_json",
    "enumerate_subsets",
    "plan_balance",
    "sha256_hex",
    "solve",
]
