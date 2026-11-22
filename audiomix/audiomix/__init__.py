"""audiomix：多采样率剪辑时间轴、真实混音档案与试听厅校准。"""
from .assets import AssetRegistry
from .timeline import TimelineEditor, Clip, ValidationError
from .engine import (
    render_reference, render_block, render_full_vectorized,
    validate_product, output_sample_count, BLOCK_SAMPLES, OUTPUT_RATE,
)
from .archive import ArchiveStore, block_key_of
from .hall import (
    CalibrationStore, HallProblem, HallSolution, SeatSpec, SpeakerOption,
    build_hall, convolve_int, deconvolve_integer, export_audition_wavs,
)

__all__ = [
    "AssetRegistry", "TimelineEditor", "Clip", "ValidationError",
    "render_reference", "render_block", "render_full_vectorized",
    "validate_product", "output_sample_count", "BLOCK_SAMPLES", "OUTPUT_RATE",
    "ArchiveStore", "block_key_of",
    "CalibrationStore", "HallProblem", "HallSolution", "SeatSpec",
    "SpeakerOption", "build_hall", "convolve_int", "deconvolve_integer",
    "export_audition_wavs",
]
