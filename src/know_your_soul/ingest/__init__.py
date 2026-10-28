"""Wave-2 ingestion package: pipeline (SUB-89), extractor (SUB-87).

The pipeline owns directory walking, incremental tracking, cascade deletes
and the proposal summary; the extractor (SUB-87) and resolver (SUB-88) are
sibling deliverables imported lazily with typed fallbacks.
"""

from know_your_soul.ingest.pipeline import (
    EXTRACTION_VERSION_FALLBACK,
    FileReport,
    IngestionBudget,
    IngestionError,
    IngestionPipeline,
    IngestionReport,
    canonical_ingestion_dump,
    detect_language,
    is_generated_hint,
)

__all__ = [
    "EXTRACTION_VERSION_FALLBACK",
    "FileReport",
    "IngestionBudget",
    "IngestionError",
    "IngestionPipeline",
    "IngestionReport",
    "canonical_ingestion_dump",
    "detect_language",
    "is_generated_hint",
]
