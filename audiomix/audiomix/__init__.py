"""audiomix：多采样率剪辑时间轴、真实混音档案与双盲听审排期。

时间轴/混音数据仓依赖 numpy/scipy；盲听刺激与排期（blindstim/
blindschedule/blindtrial）为纯标准库实现。未安装 numpy 时仅导入后者，
保证听审制作流程在最小环境下仍可运行。
"""
__all__ = [
    "AssetRegistry", "TimelineEditor", "Clip", "ValidationError",
    "render_reference", "render_block", "render_full_vectorized",
    "validate_product", "output_sample_count", "BLOCK_SAMPLES", "OUTPUT_RATE",
    "ArchiveStore", "block_key_of",
]

try:
    from .assets import AssetRegistry
    from .timeline import TimelineEditor, Clip, ValidationError
    from .engine import (
        render_reference, render_block, render_full_vectorized,
        validate_product, output_sample_count, BLOCK_SAMPLES, OUTPUT_RATE,
    )
    from .archive import ArchiveStore, block_key_of
except ModuleNotFoundError as _exc:  # 仅容忍 numpy/scipy 缺失
    if _exc.name not in ("numpy", "scipy"):
        raise
    # 静默降级：盲听模块（blindstim/blindschedule/blindtrial/blindcli）
    # 为纯标准库实现，无 numpy 环境下仍可完整使用。
