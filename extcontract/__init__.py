"""External contract toolkit: exact finite-type compatibility, adaptation
planning, file adaptation, and revision-aware atomic ingestion.

Public entry points (see README for the full walkthrough):

    load_contract / parse_contract        contract documents
    check_compatibility                   row-free whole-domain planning
    replace_field_type                    contract revisions
    adapt_rows / adapt_file_document      file -> reader-records/v1
    IngestionLibrary                      stateful, revision-aware ingestion

The supplied ``materials/745/reader_client.py`` is never imported or
modified; its CLI consumes the ``reader-records/v1`` documents this package
produces.
"""

from .types import (
    DecimalType,
    IntType,
    TextType,
    decode_writer_value,
    encode_reader_value,
    parse_type,
    rational_string,
    type_to_json,
)
from .contract import Contract, Field, load_contract, parse_contract
from .compat import CompatibilityReport, ColumnPlan, check_compatibility, replace_field_type
from .adapter import (
    AdaptationResult,
    CanonicalRow,
    RECORDS_FORMAT,
    RowError,
    adapt_file_document,
    adapt_rows,
    write_records,
)
from .ingest import IngestionLibrary, IngestResult, Receipt, RowState
from .dominoes import (
    Bridge,
    FixedRubbingContract,
    LayoutResult,
    Rubbing,
    RubbingLibrary,
    TileEdge,
    minimum_bridges_for_tracks,
    minimize_tracks,
    possible_directed_end_pairs,
    reconstruct_all,
    reconstruct_tile,
    tile_edges_from_reconstructions,
)
from .errors import (
    ContractError,
    ConversionError,
    DomainError,
    ExtContractError,
    LimitError,
)

__all__ = [
    "DecimalType",
    "IntType",
    "TextType",
    "decode_writer_value",
    "encode_reader_value",
    "parse_type",
    "rational_string",
    "type_to_json",
    "Contract",
    "Field",
    "load_contract",
    "parse_contract",
    "CompatibilityReport",
    "ColumnPlan",
    "check_compatibility",
    "replace_field_type",
    "AdaptationResult",
    "CanonicalRow",
    "RECORDS_FORMAT",
    "RowError",
    "adapt_file_document",
    "adapt_rows",
    "write_records",
    "IngestionLibrary",
    "IngestResult",
    "Receipt",
    "RowState",
    "Bridge",
    "FixedRubbingContract",
    "LayoutResult",
    "Rubbing",
    "RubbingLibrary",
    "TileEdge",
    "minimum_bridges_for_tracks",
    "minimize_tracks",
    "possible_directed_end_pairs",
    "reconstruct_all",
    "reconstruct_tile",
    "tile_edges_from_reconstructions",
    "ContractError",
    "ConversionError",
    "DomainError",
    "ExtContractError",
    "LimitError",
]

__version__ = "1.0.0"
